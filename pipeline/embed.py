"""向量层：Embedding + 余弦检索（技术方案 §2.5、§3 的 L3）

两处用途：
  1. S1 的 L3 语义近重复 —— SimHash 的指纹算的是标题+正文，不同站点的模板
     差异会让同一篇稿子指纹不同；标题精确匹配又只能抓改写前后完全一致的。
     换句话说，**改写过的转载这两层都拦不住**，只能靠语义。
  2. 事件语义检索 —— 事件向量在导出时算好进快照，线上只对用户 query 调一次
     接口，余弦在 Worker 里算。这样线上不必挂向量库。

工程约束：
  · 向量落 data/embeddings.jsonl 缓存，键是 sha1(model+task+text)。
    重跑命中缓存不重复计费，mock 模式完全零成本。
  · **int8 量化后 base64 存储**。1024 维 float 直接存 JSON 是 7KB/条，
    650 条就 4.5MB，git 扛不住；量化后 1.4KB/条，精度损失对余弦可忽略。
  · 没有 JINA_API_KEY 时全部返回 None，调用方跳过——向量是增强项，
    不能因为它缺失就让整条流水线停摆。
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import urllib.error
import urllib.request
from pathlib import Path

from pipeline.store import DATA

MODEL = os.environ.get("KB_EMBED_MODEL", "jina-embeddings-v3")
TASK = "text-matching"
DIM = 1024
CACHE = DATA / "embeddings.jsonl"
API = "https://api.jina.ai/v1/embeddings"
BATCH = 32

_mem: dict[str, list[int]] | None = None


# ------------------------------------------------------------------ 量化
def _quant(v: list[float]) -> str:
    """float[] → int8 → base64。余弦只关心方向，先归一化再量化。"""
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return base64.b64encode(
        bytes((int(round(x / n * 127)) + 128) & 0xFF for x in v)).decode()


def _dequant(b64: str) -> list[int]:
    return [b - 128 for b in base64.b64decode(b64)]


def pack(v: list[int]) -> str:
    """int8 列表 → base64。导出快照时用，与缓存里的编码保持一致。"""
    return base64.b64encode(bytes((x + 128) & 0xFF for x in v)).decode()


def cosine(a: list[int], b: list[int]) -> float:
    """量化向量的余弦。两侧都已归一化，模长相近，直接点积再除模长即可。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


# ------------------------------------------------------------------ 缓存
def _key(text: str) -> str:
    return hashlib.sha1(f"{MODEL}|{TASK}|{text}".encode()).hexdigest()[:20]


def _load() -> dict[str, list[int]]:
    global _mem
    if _mem is None:
        _mem = {}
        if CACHE.exists():
            for line in CACHE.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                    _mem[r["k"]] = _dequant(r["v"])
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    return _mem


def _append(rows: list[tuple[str, str]]) -> None:
    if not rows:
        return
    DATA.mkdir(parents=True, exist_ok=True)
    with CACHE.open("a", encoding="utf-8") as f:
        for k, b64 in rows:
            f.write(json.dumps({"k": k, "v": b64}, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ 接口
def available() -> bool:
    return bool(os.environ.get("JINA_API_KEY"))


def _call(texts: list[str]) -> list[list[float]] | None:
    key = os.environ.get("JINA_API_KEY")
    if not key:
        return None
    req = urllib.request.Request(
        API,
        data=json.dumps({"model": MODEL, "task": TASK, "input": texts}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
        out = [None] * len(texts)
        for d in body.get("data") or []:
            out[d["index"]] = d["embedding"]
        return out if all(o is not None for o in out) else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
            KeyError, IndexError):
        return None


def embed(texts: list[str]) -> list[list[int] | None]:
    """批量取向量。命中缓存的不计费；无 key 或调用失败时该条返回 None。"""
    cache = _load()
    out: list[list[int] | None] = [None] * len(texts)
    todo: list[tuple[int, str, str]] = []          # (下标, key, 文本)

    for i, t in enumerate(texts):
        t = (t or "").strip()[:1600]
        if not t:
            continue
        k = _key(t)
        if k in cache:
            out[i] = cache[k]
        else:
            todo.append((i, k, t))

    if todo and available():
        # 同一批里可能有重复文本，按 key 去重后再发
        uniq: dict[str, str] = {k: t for _, k, t in todo}
        keys = list(uniq)
        fresh: list[tuple[str, str]] = []
        for s in range(0, len(keys), BATCH):
            chunk = keys[s:s + BATCH]
            vecs = _call([uniq[k] for k in chunk])
            if not vecs:
                continue
            for k, v in zip(chunk, vecs):
                b64 = _quant(v)
                cache[k] = _dequant(b64)
                fresh.append((k, b64))
        _append(fresh)
        for i, k, _ in todo:
            if k in cache:
                out[i] = cache[k]
    return out


def nearest(vec: list[int], pool: list[tuple[str, list[int]]],
            top: int = 5, floor: float = 0.0) -> list[tuple[str, float]]:
    """朴素全量扫描。千级数据不需要向量库——650 条 × 1024 维的点积
    在 Python 里也就几十毫秒，引入 faiss/pgvector 只会增加复现门槛。"""
    if not vec:
        return []
    hits = [(i, cosine(vec, v)) for i, v in pool if v]
    hits.sort(key=lambda x: -x[1])
    return [h for h in hits[:top] if h[1] >= floor]


def stats() -> dict:
    return {"cached": len(_load()), "model": MODEL, "dim": DIM,
            "available": available()}
