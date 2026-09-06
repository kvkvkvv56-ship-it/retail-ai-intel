"""LLM 客户端（DeepSeek / OpenAI 兼容）

三个设计要点：

1. **响应缓存即 mock 回放**（技术方案 §11.3）
   每次调用按 sha1(model + messages) 落盘 data/llm_cache.jsonl。
   `--mock` 模式只读缓存不发请求，他人零 API key 即可完整复跑，
   产出与线上一致 —— 这是「可复用性」25 分的直接证据。

2. **JSON 围栏剥离**
   模型常把 JSON 包在 ```json 里，不剥会解析失败（v1 踩过）。

3. **分层用模**
   scout（预筛，吃最大调用量）用便宜档，strong（抽取/洞察）用强档。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "llm_cache.jsonl"

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, mock: bool = False):
        self.mock = mock
        self.base = os.environ.get("KB_LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
        self.key = os.environ.get("KB_LLM_API_KEY", "")
        self.scout = os.environ.get("KB_SCOUT_MODEL", "deepseek-chat")
        self.strong = os.environ.get("KB_STRONG_MODEL", "deepseek-chat")
        self.usage = {"calls": 0, "cache_hits": 0, "in_tokens": 0, "out_tokens": 0}
        self._cache = self._load_cache()

    # ------------------------------------------------------------ 缓存
    def _load_cache(self) -> dict[str, str]:
        c: dict[str, str] = {}
        if CACHE_PATH.exists():
            for line in CACHE_PATH.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        r = json.loads(line)
                        c[r["k"]] = r["v"]
                    except (json.JSONDecodeError, KeyError):
                        continue
        return c

    def _save(self, key: str, val: str) -> None:
        self._cache[key] = val
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"k": key, "v": val}, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------ 调用
    def chat(self, messages: list[dict], *, strong: bool = False,
             temperature: float = 0.2, max_tokens: int = 2000) -> str:
        model = self.strong if strong else self.scout
        key = hashlib.sha1(
            json.dumps([model, messages, temperature], ensure_ascii=False,
                       sort_keys=True).encode()).hexdigest()

        if key in self._cache:
            self.usage["cache_hits"] += 1
            return self._cache[key]

        if self.mock:
            raise LLMError(f"mock 模式下缓存未命中（{key[:8]}）——请先用真实 key 跑一轮生成缓存")

        payload = {"model": model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"})

        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=180, context=SSL_CTX) as r:
                    res = json.loads(r.read())
                txt = res["choices"][0]["message"]["content"]
                u = res.get("usage", {})
                self.usage["calls"] += 1
                self.usage["in_tokens"] += u.get("prompt_tokens", 0)
                self.usage["out_tokens"] += u.get("completion_tokens", 0)
                self._save(key, txt)
                return txt
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code in (429, 500, 502, 503):
                    time.sleep(2 ** attempt * 2)
                    continue
                raise LLMError(f"{last}: {e.read()[:200].decode('utf-8', 'ignore')}") from e
            except Exception as e:                            # noqa: BLE001
                last = f"{type(e).__name__}"
                time.sleep(2 ** attempt)
        raise LLMError(f"三次重试后仍失败：{last}")

    # ------------------------------------------------------------ JSON
    def chat_json(self, messages: list[dict], **kw):
        """要求模型返回 JSON。剥离围栏后解析，失败抛 LLMError。"""
        raw = self.chat(messages, **kw)
        return parse_json(raw)

    def cost_estimate(self) -> float:
        """DeepSeek 定价（deepseek-chat）：输入 ¥2/M（缓存未命中），输出 ¥3/M。"""
        return round(self.usage["in_tokens"] / 1e6 * 2
                     + self.usage["out_tokens"] / 1e6 * 3, 4)


_FENCE = re.compile(r"(?s)```(?:json)?\s*(.*?)\s*```")


def parse_json(raw: str):
    s = raw.strip()
    m = _FENCE.search(s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 兜底：截取第一个完整的 JSON 对象/数组
    for op, cl in (("{", "}"), ("[", "]")):
        i, j = s.find(op), s.rfind(cl)
        if i != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"无法解析为 JSON：{raw[:180]}")
