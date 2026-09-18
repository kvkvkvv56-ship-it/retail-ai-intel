"""S1b 语义去重 · S2 预筛 · S3 抽取 · S4 归并 · S5 核验

S1b/S2/S3/S4 用 LLM，S5 是纯规则。
分工原则（技术方案 §0）：**模型只做抽取，可信度由规则裁决。**
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from pipeline import embed as EMB
from pipeline import prompts as P
from pipeline.llm import LLMError

STAGE_ORDER = {"概念宣传": 0, "试点探索": 1, "已上线": 2, "规模化应用": 3}
CLASS_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}

# 未登记信源归组用。社交平台上的不同账号是不同主体，按「域名 + 账号」区分；
# 其余一律归到可注册域名，避免子域名（stock./field./m.）被当成不同机构。
_SOCIAL_HOSTS = {"x.com", "twitter.com", "weibo.com", "weibo.cn",
                 "linkedin.com", "t.me", "youtube.com", "bilibili.com"}
# 两段公共后缀，用于从 host 里切出可注册域名（仅标准库，不引 publicsuffix）
_MULTI_SUFFIX = {"com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
                 "com.hk", "com.tw", "com.sg", "com.au", "co.uk", "co.jp", "co.kr"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# =================================================================== S1b
# 条目层语义去重（技术方案 §3 的 L3）。
#
# S1 的三层全是字面匹配：L1 比 URL、L2a 比归一化标题、L2b 比正文 SimHash。
# 改写过的转载三层都拦不住——换了标题、重排了段落，指纹就不同了。这些条目
# 会一路走到 S3 花掉强档抽取的钱，最后在 S4b 的事件层被合掉。
#
# 本阶段把这道拦截提前到 S2 之前：向量召回高相似条目对，**便宜档逐对裁决**。
# 为什么不像 L1/L2 那样阈值直接判：字面近重复几乎必然是转载，语义近重复不是
# ——两家媒体各自采写同一事件，向量同样很高，而那恰恰是多源印证。见
# prompts.SYNDICATION_SYS 的说明。判错一次，一个事件就永远停在「单源待确认」。
DEFAULT_L3 = {"enabled": True, "window_days": 14, "pool_max": 400,
              "min_cosine": 0.90, "topk": 3, "batch": 6}


def _l3_text(title: str, content: str | None) -> str:
    return f"{title}。{(content or '')[:600]}"


def stage_dedup_semantic(store, llm, cfg: dict, run_id: str) -> dict:
    """S1b 条目层语义去重：向量召回 + 便宜档逐对裁决。

    比对池刻意只取最近 window_days 天、且至多 pool_max 条：转载是跟着原稿
    走的，隔了两周还在转的极少，而池子每扩一条就多一次向量调用。首轮会把
    池子整个算一遍，之后命中缓存，每轮只为新条目付费。

    缺 JINA_API_KEY 时整段跳过，行为与本阶段不存在时完全一致。
    """
    conf = {**DEFAULT_L3, **(cfg.get("semantic_dedup") or {})}
    stats = {"input": 0, "pool": 0, "recall_pairs": 0, "dup": 0,
             "kept": 0, "error": 0, "skipped": None}
    if not conf["enabled"]:
        stats["skipped"] = "配置关闭"
        return stats
    if not EMB.available():
        stats["skipped"] = "缺 JINA_API_KEY，跳过语义去重（改写转载将在 S4b 事件层兜底）"
        return stats

    fresh = [dict(r) for r in store.q(
        "SELECT * FROM items WHERE status='pending' ORDER BY id")]
    stats["input"] = len(fresh)
    if not fresh:
        return stats

    since = (datetime.now(timezone.utc)
             - timedelta(days=conf["window_days"])).isoformat()
    fresh_ids = {r["id"] for r in fresh}
    pool = [dict(r) for r in store.q(
        """SELECT * FROM items WHERE status NOT IN ('rejected')
           AND COALESCE(published_at, discovered_at) >= ?
           ORDER BY COALESCE(published_at, discovered_at) DESC LIMIT ?""",
        (since, conf["pool_max"]))]
    pool = [r for r in pool if r["id"] not in fresh_ids]
    stats["pool"] = len(pool)

    # 新条目排在前面，下标 0..len(fresh)-1 即 focus 集合
    rows = fresh + pool
    vecs = EMB.embed([_l3_text(r["title"], r["content"]) for r in rows])
    pairs = EMB.band_pairs(vecs, conf["min_cosine"], focus=set(range(len(fresh))),
                           top=conf["topk"])
    stats["recall_pairs"] = len(pairs)
    if not pairs:
        return stats

    src_name = {s["id"]: s["name"] for s in store.q("SELECT id, name FROM sources")}

    def side(r: dict) -> dict:
        return {"title": r["title"],
                "source": src_name.get(r["source_id"] or "", "未登记来源"),
                "date": (r["published_at"] or r["discovered_at"] or "")[:10],
                "text": (r["content"] or "")[:420].replace("\n", " ")}

    # 已在本阶段判掉的不再作为存活方参与后续对——A 并进 B 之后，
    # 若 B 又被判给 C，A 的台账会指向一个已经作废的条目
    dropped: set[str] = set()
    batches = [pairs[i:i + conf["batch"]]
               for i in range(0, len(pairs), conf["batch"])]

    for batch in batches:
        payload = [{"sim": c, "a": side(rows[a]), "b": side(rows[b])}
                   for a, b, c in batch]
        try:
            res = llm.chat_json([
                {"role": "system", "content": P.SYNDICATION_SYS},
                {"role": "user", "content": P.syndication_user(payload)},
            ], max_tokens=900)
        except Exception:                                     # noqa: BLE001
            stats["error"] += 1
            continue
        if isinstance(res, dict):
            res = res.get("results") or res.get("pairs") or []
        verdicts = {v.get("i"): v for v in res if isinstance(v, dict)}

        for j, (a, b, c) in enumerate(batch):
            v = verdicts.get(j)
            if not v or not v.get("dup"):
                stats["kept"] += 1
                continue
            # a 一定来自 focus（新条目）；b 可能是池里的老条目，也可能是本轮另一条。
            # 无论哪种，丢掉更晚发现的那条，留下先入库的——与 L2 的取向一致。
            new, old = (rows[a], rows[b]) if rows[a]["discovered_at"] >= \
                rows[b]["discovered_at"] else (rows[b], rows[a])
            if new["id"] in dropped or old["id"] in dropped:
                continue
            dropped.add(new["id"])
            store.db.execute("UPDATE items SET status='rejected' WHERE id=?",
                             (new["id"],))
            store.insert("rejects", {
                "run_id": run_id, "item_id": new["id"], "url": new["url"],
                "title": new["title"][:200],
                "source_name": src_name.get(new["source_id"] or "", "未登记"),
                "stage": "semantic_dedup", "reason_code": "syndication",
                "reason_detail": f"与 {old['id']} 语义近重复（余弦 {c:.3f}），"
                                 f"模型判定为同一篇稿件的转载/改写：{v.get('why', '')}",
                "merged_into": old["id"], "created_at": _now()})
            stats["dup"] += 1

    store.commit()
    return stats


# ==================================================================== S2
def stage_prescreen(store, llm, cfg: dict, run_id: str, batch_size: int = 12) -> dict:
    """便宜模型批量预筛。吃最大调用量，所以用便宜档。"""
    items = [dict(r) for r in store.q(
        "SELECT * FROM items WHERE status='pending' ORDER BY id")]
    stats = {"input": len(items), "keep": 0, "drop": 0, "error": 0}
    if not items:
        return stats

    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]

    def one(batch):
        try:
            res = llm.chat_json([
                {"role": "system", "content": P.prescreen_sys(cfg)},
                {"role": "user", "content": P.prescreen_user(batch)},
            ], max_tokens=1200)
            if isinstance(res, dict):
                res = res.get("results") or res.get("items") or []
            return batch, res, None
        except (LLMError, Exception) as e:                    # noqa: BLE001
            return batch, None, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=4) as ex:
        for f in as_completed([ex.submit(one, b) for b in batches]):
            batch, res, err = f.result()
            if err:
                stats["error"] += len(batch)
                continue
            verdicts = {v.get("i"): v for v in res if isinstance(v, dict)}
            for i, it in enumerate(batch):
                v = verdicts.get(i)
                if v is None or v.get("keep"):
                    store.db.execute("UPDATE items SET status='screened' WHERE id=?",
                                     (it["id"],))
                    stats["keep"] += 1
                else:
                    store.db.execute("UPDATE items SET status='rejected' WHERE id=?",
                                     (it["id"],))
                    store.insert("rejects", {
                        "run_id": run_id, "item_id": it["id"], "url": it["url"],
                        "title": it["title"][:200], "source_name": it["source_id"] or "未登记",
                        "stage": "prescreen", "reason_code": "irrelevant",
                        "reason_detail": f"LLM 预筛判定不属于观察范围：{v.get('why', '')}",
                        "created_at": _now(),
                    })
                    stats["drop"] += 1
    store.commit()
    return stats


# ==================================================================== S3
def stage_extract(store, llm, cfg: dict, run_id: str, srccfg: dict) -> dict:
    """强模型结构化抽取。提示词按信源一手性分流（技术方案 §3.2）。"""
    src_by_id = {s["id"]: s for s in srccfg["sources"]}
    items = [dict(r) for r in store.q(
        "SELECT * FROM items WHERE status='screened' ORDER BY id")]
    stats = {"input": len(items), "extracted": 0, "skipped_D": 0,
             "irrelevant": 0, "error": 0,
             "facts": 0, "inferences": 0, "dropped_claims": 0}

    def one(it):
        cls = it.get("effective_class") or "D"
        if cls == "D":
            return it, None, "skip_D"
        if cls not in ("A", "B", "C", "E"):
            return it, None, "skip_D"
        sys_prompt = P.extract_sys(cfg, cls)
        sname = src_by_id.get(it["source_id"], {}).get("name", "未登记来源")
        try:
            return it, llm.chat_json([
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": P.extract_user(it, sname)},
            ], strong=True, max_tokens=1500), None
        except Exception as e:                                # noqa: BLE001
            return it, None, f"{type(e).__name__}"

    with ThreadPoolExecutor(max_workers=4) as ex:
        for f in as_completed([ex.submit(one, it) for it in items]):
            it, res, err = f.result()

            if err == "skip_D":
                # D 类（聚合转载）不抽事实，只作为归并证据保留
                store.db.execute("UPDATE items SET status='evidence_only' WHERE id=?",
                                 (it["id"],))
                stats["skipped_D"] += 1
                continue
            if err or not res:
                stats["error"] += 1
                continue
            if not res.get("relevant", True) or not res.get("event_key"):
                store.db.execute("UPDATE items SET status='rejected' WHERE id=?", (it["id"],))
                store.insert("rejects", {
                    "run_id": run_id, "item_id": it["id"], "url": it["url"],
                    "title": it["title"][:200], "source_name": it["source_id"] or "未登记",
                    "stage": "extract", "reason_code": "irrelevant",
                    "reason_detail": "抽取阶段判定不构成可追踪事件",
                    "created_at": _now(),
                })
                stats["irrelevant"] += 1
                continue

            # 抽取结果暂存在 items 上，S4 归并后才建 event。
            # draft_stage 单独落一列：event_id 上的 draft 前缀会被 S4 覆盖，
            # 而 S5 每轮都要对**全库**事件重算阶段——只留在内存里，
            # 老事件下一轮就会被重算成「信息不足」。
            store.db.execute(
                "UPDATE items SET status='extracted', event_id=?, draft_stage=? WHERE id=?",
                ("draft:" + json.dumps(res, ensure_ascii=False),
                 res.get("stage") if res.get("stage") in STAGE_ORDER else None,
                 it["id"]))
            stats["extracted"] += 1
            stats["facts"] += len(res.get("facts") or [])
            stats["inferences"] += len(res.get("inferences") or [])

    store.commit()
    return stats


# ==================================================================== S4
def stage_merge(store, llm, cfg: dict, run_id: str) -> dict:
    """事件归并：按公司分组，模型通读该公司全部候选后聚类。

    技术方案 §5.1 原设计为 embedding Top-K 召回 + LLM 裁决。实际实现改为
    「按公司分组整体通读」，原因：
      1. DeepSeek 无 embedding 接口，引入第二家供应商会增加密钥与复现成本
      2. 本量级下每家公司候选仅 20-30 条，模型一次通读即可，召回率反而更高
         （embedding 会漏掉措辞差异大的同一事件）
      3. 模型必须给出归并理由，理由入库可审计 —— 这是 embedding 给不了的
    """
    comp_name = {c["id"]: c["name"] for c in cfg["companies"]}
    valid = set(comp_name)
    # 模型会输出体系外的主体（如 1688、阿里云），归到所属集团；无法归属的进 industry
    # 别名同样来自 config：每个行业的子品牌归属完全不同，
    # 硬编码等于把这套流水线焊死在电商上
    alias = {k.lower(): v for k, v in (cfg.get("_别名") or {}).items()}
    rows = store.q("SELECT * FROM items WHERE status='extracted' ORDER BY id")

    by_company: dict[str, list[dict]] = {}
    for r in rows:
        d = json.loads(r["event_id"][6:])
        d["_item_id"] = r["id"]
        d["_published_at"] = r["published_at"]
        c = (d.get("company") or "industry").strip().lower()
        c = c if c in valid else alias.get(c, "industry")
        d["company"] = c
        by_company.setdefault(c, []).append(d)

    stats = {"companies": len(by_company), "candidates": len(rows),
             "events": 0, "merged_away": 0, "relations": 0,
             "orphan_rescued": 0, "error": 0}

    def one(company, cands):
        if len(cands) == 1:
            return company, cands, {"groups": [{
                "canonical_key": cands[0]["event_key"],
                "title": cands[0].get("event_title") or cands[0]["event_key"],
                "members": [0], "reason": "本公司本轮仅一条候选，无需归并"}],
                "relations": []}, None
        try:
            return company, cands, llm.chat_json([
                {"role": "system", "content": P.MERGE_SYS},
                {"role": "user", "content": P.merge_user(
                    comp_name.get(company, company), cands)},
            ], strong=True, max_tokens=2500), None
        except Exception as e:                                # noqa: BLE001
            return company, cands, None, f"{type(e).__name__}"

    with ThreadPoolExecutor(max_workers=3) as ex:
        for f in as_completed([ex.submit(one, c, v) for c, v in by_company.items()]):
            company, cands, res, err = f.result()
            if err or not res:
                # 归并调用失败时不能把这批候选留在 extracted 状态静默丢失——
                # 它们已经过预筛与抽取，是有效数据。降级为「每条独立成事件」，
                # 宁可事件碎一些，也不能凭空少掉。
                # （实测这个泄漏让事件数从 129 掉到 79，51 条卡在 extracted）
                stats["error"] += 1
                res = {"groups": [{"canonical_key": c["event_key"],
                                   "title": c.get("event_title") or c["event_key"],
                                   "members": [i],
                                   "reason": "归并调用失败，降级为独立事件"}
                                  for i, c in enumerate(cands)],
                       "relations": []}
                stats["merge_fallback"] = stats.get("merge_fallback", 0) + len(cands)

            # 模型在 relations 里引用的 event_key，可能是归并后的 canonical_key，
            # 也可能是某个成员原本的 key。两者都登记，下面按它解析边的端点。
            key2eid: dict[str, str] = {}

            for g in res.get("groups", []):
                members = [cands[i] for i in g.get("members", [])
                           if isinstance(i, int) and 0 <= i < len(cands)]
                if not members:
                    continue
                key = g.get("canonical_key") or members[0]["event_key"]
                eid = "EV-" + hashlib.sha1(f"{company}:{key}".encode()).hexdigest()[:8]
                key2eid[key] = eid
                for m in members:
                    key2eid.setdefault(m["event_key"], eid)

                dates = [m.get("event_date") or (m.get("_published_at") or "")[:10]
                         for m in members]
                dates = sorted(d for d in dates if d and d != "None")
                doms = sorted({d for m in members for d in (m.get("domains") or [])})

                prev = store.q("SELECT first_seen_at FROM events WHERE id=?", (eid,))
                store.upsert("events", {
                    "id": eid, "event_key": key,
                    "title": g.get("title") or members[0].get("event_title") or key,
                    "summary": members[0].get("summary"),
                    "company": company, "domains": doms,
                    "first_seen_at": prev[0]["first_seen_at"] if prev else _now(),
                    "last_update_at": _now(),
                    "event_date": dates[0] if dates else None,
                })
                stats["events"] += 1
                stats["merged_away"] += len(members) - 1

                for m in members:
                    store.db.execute(
                        "UPDATE items SET event_id=?, status='accepted' WHERE id=?",
                        (eid, m["_item_id"]))
                    _write_claims(store, eid, m)

                if len(members) > 1:
                    store.insert("rejects", {
                        "run_id": run_id, "item_id": None, "url": None,
                        "title": g.get("title"), "source_name": "—",
                        "stage": "merge", "reason_code": "merged",
                        "reason_detail": f"{len(members)} 条报道归并为 1 个事件：{g.get('reason', '')}",
                        "merged_into": eid, "created_at": _now(),
                    })

            # 修复：LLM 未分组的候选不能静默丢弃，各自独立成事件
            assigned = {i for g in res.get("groups", [])
                        for i in g.get("members", []) if isinstance(i, int)}
            for i, m in enumerate(cands):
                if i in assigned:
                    continue
                key = m["event_key"]
                eid = "EV-" + hashlib.sha1(f"{company}:{key}".encode()).hexdigest()[:8]
                key2eid.setdefault(key, eid)
                prev = store.q("SELECT first_seen_at FROM events WHERE id=?", (eid,))
                store.upsert("events", {
                    "id": eid, "event_key": key,
                    "title": m.get("event_title") or key,
                    "summary": m.get("summary"), "company": company,
                    "domains": m.get("domains") or [],
                    "first_seen_at": prev[0]["first_seen_at"] if prev else _now(),
                    "last_update_at": _now(),
                    "event_date": m.get("event_date")
                                  or (m.get("_published_at") or "")[:10] or None,
                })
                store.db.execute(
                    "UPDATE items SET event_id=?, status='accepted' WHERE id=?",
                    (eid, m["_item_id"]))
                _write_claims(store, eid, m)
                stats["events"] += 1
                stats["orphan_rescued"] += 1

            # 关系边的端点必须解析到**本轮真的写进库的事件**。
            #
            # 原实现直接拿模型给的 key 算 sha1 当事件 id，不校验它是否存在。
            # 模型很容易在 relations 里引用一个它自己临时编的 key（或者被归并
            # 改写掉的旧 key），于是边指向一个从未存在过的 EV 编号：实测库里
            # 4 条模型边有 2 条是这样的悬空边——一半。前端拿不到标题，只能把
            # 原始编号打出来，点进去是空页；知识图谱里则是一条连向虚空的线。
            for rel in res.get("relations", []):
                a, b = key2eid.get(rel.get("from")), key2eid.get(rel.get("to"))
                if not a or not b or a == b or not rel.get("basis"):
                    stats["relations_dropped"] = stats.get("relations_dropped", 0) + 1
                    continue
                store.insert("edges", {
                    "from_event": a, "to_event": b,
                    "relation": rel.get("relation") or "follows",
                    "basis": rel["basis"], "created_by": "llm"})
                stats["relations"] += 1

    store.commit()
    return stats


# 向量召回阈值。事件两两余弦的中位数是 0.560、p99 是 0.807；取 0.80 时
# 候选对占全部的 1.1%，而相似度最高的若干对经人工核对全部是真重复
# （其中一对只差引号：「"AI万相"」与「“AI万相”」）。
# 这是召回阈值不是判定阈值——判定由 LLM 做，所以宁可宽一点。
XMERGE_FLOOR = 0.80
XMERGE_BATCH = 10


def _event_vec_text(store, e) -> str:
    facts = store.q("""SELECT text FROM claims WHERE event_id=? AND kind='fact'
                       LIMIT 2""", (e["id"],))
    return (f'{e["title"]}。{(e["summary"] or "")[:200]}'
            + "".join(f' {f["text"][:80]}' for f in facts))


def event_vectors(store, evs: list) -> list[list[int] | None] | None:
    """事件向量。文本构造与 export.py 的线上检索向量**逐字一致**——
    一条事件因此只算一次、只付一次费，S4b、S6b 与线上检索三处共用。
    无 embedding 能力或几乎全部取不到时返回 None，调用方各自降级。"""
    if not EMB.available():
        return None
    vecs = EMB.embed([_event_vec_text(store, e) for e in evs])
    return vecs if sum(1 for v in vecs if v) >= 2 else None


def _xmerge_pairs(store, evs: list) -> list[tuple[int, int, float]] | None:
    """向量召回：返回值得送去裁决的事件对。无 embedding 能力时返回 None。"""
    vecs = event_vectors(store, evs)
    if vecs is None:
        return None
    return EMB.band_pairs(vecs, XMERGE_FLOOR)


def stage_merge_cross(store, llm, run_id: str) -> dict:
    """S4b 全库归并复核：**向量召回 + LLM 裁决**。

    补的是 S4 的两个结构性盲区。S4 只读本轮 status='extracted' 的候选、
    并且按 company 分组，于是：

    1. **跨轮次看不见。** 事件的跨轮同一性完全靠 LLM 每次生成一模一样的
       event_key 字符串（eid = sha1(company:key)）。同一条新闻隔天再被报道，
       模型给出 `wechat-ai-agent-weilm` 和 `wechat-xiaowei-ai-social`，
       差一个词就是两个事件。实测 09-07 与 09-08 的「微信小微」即此情形，
       两者余弦 0.869，远在召回阈值之上。这是本阶段最主要的价值——
       让库里同一件事**只保留第一次出现的那条**，后续报道并入它作为佐证。
    2. **跨公司看不见。** 同一事件被抽成不同 company 时永不相遇。首轮实跑
       即命中：「千问AI Arena」判为 taobao、「阿里云通义AI竞技场」判为
       industry，实为同一件事。（这个漏归并是周报的 method_notes 自己报的。）

    原实现把**全部事件**塞进一次调用整体扫描。两个问题：
      1. 不可扩展 —— 百级事件的提示词已经很长，五百级就塞不下
      2. 实测漏判严重 —— 事件涨到 133 个之后，余弦最高的 8 对全是真重复，
         而整表通读一对都没抓到，其中还有一对标题只差一个引号

    改为：向量召回出高相似度的事件对（占全部两两组合约 1%），分批送模型
    逐对裁决。这正是技术方案原本写的「embedding + LLM 裁决」。
    向量不可用时退回整表通读，行为与改动前一致。

    规模上限：两两余弦是纯 Python 的 O(n²)，实测 178 事件 15753 对耗时
    1.5s（93 µs/对）。外推 1000 事件约 47s、2000 约 3 分钟、5000 约 19 分钟——
    作业超时是 25 分钟，所以天花板在 4000-5000 事件。到那时需要换成
    分块比对或近似最近邻，不能再全量两两算。
    """
    evs = store.q("SELECT id, company, event_key, title, summary, "
                  "first_seen_at, event_date FROM events")
    stats = {"scanned": len(evs), "cross_merged": 0, "suspects": 0, "error": 0,
             "recall_pairs": 0, "recall": "full_scan", "split_blocked": 0}
    if len(evs) < 2:
        return stats

    def block(i: int) -> str:
        e = evs[i]
        facts = store.q("""SELECT text FROM claims WHERE event_id=?
                           AND kind='fact' LIMIT 2""", (e["id"],))
        return (f'[{i}] {e["company"]} | {e["event_key"]}\n'
                f'    标题：{e["title"]}\n'
                f'    概述：{(e["summary"] or "")[:100]}\n'
                + "".join(f'    · {f["text"][:90]}\n' for f in facts))

    pairs = _xmerge_pairs(store, evs)
    res = {"duplicates": [], "suspects": []}

    if pairs is None:
        # 退化路径：整表通读（旧行为）
        try:
            res = llm.chat_json([
                {"role": "system", "content": P.MERGE_CROSS_SYS},
                {"role": "user", "content": "全部事件：\n\n"
                    + "\n\n".join(block(i) for i in range(len(evs)))},
            ], strong=True, max_tokens=1500)
        except Exception:                                     # noqa: BLE001
            stats["error"] = 1
            return stats
    else:
        stats["recall"] = "vector"
        stats["recall_pairs"] = len(pairs)
        if not pairs:
            return stats
        for s0 in range(0, len(pairs), XMERGE_BATCH):
            chunk = pairs[s0:s0 + XMERGE_BATCH]
            idx = sorted({i for a, b, _ in chunk for i in (a, b)})
            listing = "\n\n".join(block(i) for i in idx)
            asks = "\n".join(f"- [{a}] 与 [{b}]（向量相似度 {c:.2f}）"
                              for a, b, c in chunk)
            try:
                r = llm.chat_json([
                    {"role": "system", "content": P.MERGE_CROSS_SYS},
                    {"role": "user", "content":
                        f"以下是向量召回出的疑似重复事件对，请逐对裁决：\n{asks}"
                        f"\n\n涉及事件的详情：\n\n{listing}"},
                ], strong=True, max_tokens=1200)
            except Exception:                                 # noqa: BLE001
                stats["error"] += 1
                continue
            res["duplicates"].extend(r.get("duplicates") or [])
            res["suspects"].extend(r.get("suspects") or [])

    # 人裁定为「不同事件」的组，模型不得再合。
    #
    # split 写的是 review_state='confirmed'，而 confirmed 不在归并的排除条件里，
    # 于是人的判断会在下一轮被无声推翻——实测 36 组 split 裁决里有 12 组的成员
    # 已被后续归并吃掉，其中 4 组是直接合成了当初判开的那个对家，最快的一组
    # 间隔 28 分钟；「快手可灵」那条产品线更是被一路归并成单个事件，而 2024 年
    # 的模型发布与 2026 年的营收数据本来就是人明确判过「不同阶段不该合」的。
    #
    # 文档里说的「复核状态具备粘性」此前只覆盖 stage_verify 不把裁决冲回
    # pending，归并这条路径是漏的，而它推翻的恰恰是裁决本身。HITL 的意义在于
    # 人的判断能压过模型，压不住就不成其为 HITL。
    split_pairs = {frozenset(r["target_id"].split("|")) for r in
                   store.q("SELECT target_id FROM reviews WHERE action='split'")}

    # 分批裁决后，前一批可能已经把某个事件合掉了。后一批若还引用它，
    # 会把条目挂到一个已删除的事件上——必须跳过已消失的成员。
    gone: set[str] = set()
    for g in res.get("duplicates") or []:
        idx = [i for i in g.get("members", [])
               if isinstance(i, int) and 0 <= i < len(evs)
               and evs[i]["id"] not in gone]
        if len(idx) < 2:
            continue
        members = [evs[i] for i in idx]

        # 组里只要有两个成员被人判过「不是一回事」，整组都不合——
        # 模型是把这些当成同一个事件提出来的，其中一对既然被否掉，
        # 这个分组本身就不可信。放回队列比合错强，合错不可逆。
        mids = {m["id"] for m in members}
        blocked = next((p for p in split_pairs if len(p & mids) >= 2), None)
        if blocked:
            stats["split_blocked"] += 1
            stats.setdefault("split_blocked_pairs", []).append("|".join(sorted(blocked)))
            continue

        # 保留**最早进库**的那条作为主事件。
        #
        # 原规则是「留信源最多的那条」，方向错了：同一条新闻第二天被另一家
        # 转述、抽成新事件时，新的那条往往当轮信源更多，于是把最早那条删掉，
        # 事件的 first_seen_at 和 event_date 一起被推后——库里就再也看不出
        # 这件事是什么时候第一次出现的，而这恰恰是情报库的核心价值。
        # 信源不会因此丢失：下面把被合并方的 items/claims 全部改挂到 keep 上，
        # S5 会按合并后的 items 重算独立信源数，置信度该升还是会升。
        counts = {m["id"]: store.one(
            "SELECT COUNT(*) FROM items WHERE event_id=?", (m["id"],)) or 0
            for m in members}
        # 同一时刻进库时（同一轮抽出的两条）再看信源数，多的那条更完整
        keep = min(members, key=lambda m: (m["first_seen_at"] or "9999",
                                           -counts[m["id"]]))
        for m in members:
            if m["id"] == keep["id"]:
                continue
            store.db.execute("UPDATE items SET event_id=? WHERE event_id=?",
                             (keep["id"], m["id"]))
            store.db.execute("UPDATE claims SET event_id=? WHERE event_id=?",
                             (keep["id"], m["id"]))
            store.db.execute("DELETE FROM edges WHERE from_event=? OR to_event=?",
                             (m["id"], m["id"]))
            store.db.execute("DELETE FROM events WHERE id=?", (m["id"],))
            gone.add(m["id"])
            # 链式合并会把审计链切断：前一批把 X 合进 m，这一批又把 m 合进 keep，
            # 于是「X 去哪了」的台账行指向一个已被删除的事件。实测本轮 24 次
            # 合并里出现 1 次，历史数据里也有 3 条这样的断链。改指到新的幸存者——
            # 条目和 claim 本来就是链式迁移的，台账不该比它们少一环
            store.db.execute("UPDATE rejects SET merged_into=? "
                             "WHERE stage='merge_cross' AND merged_into=?",
                             (keep["id"], m["id"]))
            store.insert("rejects", {
                "run_id": run_id, "item_id": None, "url": None,
                "title": m["title"], "source_name": "—",
                "stage": "merge_cross", "reason_code": "merged",
                "reason_detail": f"全库复核：与更早入库的 {keep['id']} 为同一事件，"
                                 f"证据已并入。{g.get('reason', '')}",
                "merged_into": keep["id"], "created_at": _now()})
            stats["cross_merged"] += 1

        # 同一件事被抽成两条时，event_date 也可能不同（实测：微信小微那条
        # 09-07 抽成 09-07、09-08 又抽成 09-08）。合并后归到最早的那个，
        # 否则事件会显示成比它实际发生时间更晚。first_seen_at 不用动——
        # keep 选的就是最早进库的那条。last_update_at 刷新，因为确实有新证据进来
        dates = sorted(d for d in (m["event_date"] for m in members) if d)
        if dates and dates[0] != keep["event_date"]:
            store.db.execute("UPDATE events SET event_date=? WHERE id=?",
                             (dates[0], keep["id"]))
        store.db.execute("UPDATE events SET last_update_at=? WHERE id=?",
                         (_now(), keep["id"]))

    # 模型表达的不确定性 → 人工复核队列（不自动合并）。
    # 两条标记路径共用一个 seen，彼此之间也不会把同一组记两遍
    seen = _flagged_pairs(store)
    for g in res.get("suspects") or []:
        idx = [i for i in g.get("members", [])
               if isinstance(i, int) and 0 <= i < len(evs)
               and evs[i]["id"] not in gone]
        if len(idx) < 2:
            continue
        ms = [evs[i] for i in idx]
        for m in ms:
            store.db.execute(
                "UPDATE events SET review_state='suspect_duplicate' "
                "WHERE id=? AND review_state IN ('none','')", (m["id"],))
        stats["suspects"] += _flag_once(
            store, seen, [m["id"] for m in ms],
            "模型标记疑似重复，证据不足未自动合并：" + g.get("reason", "")
            + "｜涉及：" + " / ".join(f'{m["title"][:26]}({m["company"]})'
                                      for m in ms))

    stats["suspects"] += _flag_suspect_duplicates(store, seen=seen)
    # 本轮归并可能吃掉了某个标记组的另一方，顺手把幸存一方放出队列
    stats["stale_suspects"] = _clear_stale_suspects(store)
    store.commit()
    return stats


def _clear_stale_suspects(store) -> int:
    """标记组里的另一方被后续自动归并吃掉后，撤掉幸存一方的 suspect_duplicate。

    重复问题此时已经不存在了——那一方已经并进别的事件——但状态没人撤，
    事件就永远卡在复核队列的「疑似重复」里，而队列里又找不到可比对的对象
    （实测卡住 4 个，其中最早的从 2025 年就挂着）。stage_verify 把
    suspect_duplicate 列为粘性状态不动它，于是谁也不会把它放出来。

    系统设的状态由系统撤，人裁过的（watching/confirmed/rejected）不在此列，
    粘性规则不变。撤回 none 之后，下一轮 verify 会按置信度重新判它该不该
    进单源队列——该进的照样进，不该进的就此出队。
    """
    alive = {r["id"] for r in store.q("SELECT id FROM events")}
    live_flagged = set()
    for f in store.q("SELECT target_id FROM reviews WHERE action='flag'"):
        ids = f["target_id"].split("|")
        if all(i in alive for i in ids):
            live_flagged.update(ids)
    stale = [r["id"] for r in
             store.q("SELECT id FROM events WHERE review_state='suspect_duplicate'")
             if r["id"] not in live_flagged]
    for i in stale:
        store.db.execute("UPDATE events SET review_state='none' WHERE id=?", (i,))
    return len(stale)


def _flagged_pairs(store) -> set[frozenset]:
    """已经标记过、或人已经裁决过的疑似重复组。成员顺序不同视为同一组。

    裁决过的（split / same）也算在内：人已经回答过的问题不该再问一遍，
    否则队列里会反复冒出同一组，而它早就有结论了。
    """
    return {frozenset(r["target_id"].split("|")) for r in store.q(
        "SELECT target_id FROM reviews WHERE action IN ('flag','split','same')")}


def _flag_once(store, seen: set, ids: list[str], note: str) -> int:
    """同一组疑似重复只记一次账。

    两处 flag 原本每轮无条件 insert，同一组被反复记录：实测 390 条 flag 只
    对应 131 组不同的对，最多的一组记了 13 次。代价不止账本变胖——复核队列
    里同一组要翻十几遍（显示 176 组待裁决，去重后其实只有 32 组），
    「复核记录」的计数也被灌水（408 条里 390 条是系统自己重复写的，而这个
    数字会展示在线上信源页）。

    标记表达的是「这组待人裁决」这个**状态**，不是流水，记一次就够；
    真正的流水是人的裁决（split / same），那个本来就每次都记。
    """
    key = frozenset(ids)
    if key in seen:
        return 0
    seen.add(key)
    store.insert("reviews", {
        "target_type": "merge", "target_id": "|".join(ids), "action": "flag",
        "note": note, "reviewer": "system", "created_at": _now()})
    return 1


def _flag_suspect_duplicates(store, jaccard: float = 0.34,
                             seen: set | None = None) -> int:
    """标记疑似重复事件，进人工复核队列。

    S4b 的提示词要求「宁可漏掉也不要误合并」——误合并会不可逆地毁掉信息，
    漏合并只是冗余且可修。所以自动归并刻意保守，代价是会留下漏网。

    此处用零成本的标题词重合度做疑似标记，不自动合并，只挂进人工队列。
    这是 HITL「归并纠错」节点（技术方案 §12）的输入来源。
    """
    import re
    if seen is None:                      # 单独调用时自己取一次
        seen = _flagged_pairs(store)
    evs = store.q("SELECT id, title, company FROM events")

    def toks(t: str) -> set[str]:
        """滑动窗口二元组。

        注意：不能用 re.findall(r"[一-鿿]{2}") —— 那取的是**不重叠**二元组，
        偏移不同的两个标题即使含相同词也对不上。实测「聚焦跨境」在一个标题里
        切成 聚焦|跨境，另一个切成 场聚|焦跨|境电，交集为零。
        """
        cn = re.sub(r"[^\u4e00-\u9fff]", "", t)
        grams = {cn[i:i + 2] for i in range(len(cn) - 1)}
        return grams | {w.lower() for w in re.findall(r"[A-Za-z]{3,}", t)}
    n = 0
    for i, a in enumerate(evs):
        for b in evs[i + 1:]:
            A, B = toks(a["title"]), toks(b["title"])
            if not A or not B:
                continue
            if len(A & B) / len(A | B) >= jaccard:
                for e in (a, b):
                    store.db.execute(
                        "UPDATE events SET review_state='suspect_duplicate' "
                        "WHERE id=? AND review_state IN ('none','')", (e["id"],))
                n += _flag_once(
                    store, seen, [a["id"], b["id"]],
                    f"疑似重复待人工判定：「{a['title'][:30]}」({a['company']}) "
                    f"与「{b['title'][:30]}」({b['company']}) 标题词高度重合，"
                    f"自动归并未合并（保守策略）")
    return n


def _write_claims(store, event_id: str, data: dict) -> None:
    """写入 claims，**程序层强制约束**（技术方案 §7）。

    模型输出不合规就丢弃，不写库：
      fact          必须有 source_item_id；E 类信源不得产出 fact
      inference     必须有 attributed_to 或 based_on
      recommendation 不得有 source_item_id（建议不能有信源）
    """
    item_id = data["_item_id"]
    for txt in data.get("facts") or []:
        if not isinstance(txt, str) or len(txt.strip()) < 6:
            continue
        cid = "cl_" + hashlib.sha1(f"{event_id}{item_id}f{txt}".encode()).hexdigest()[:12]
        store.upsert("claims", {
            "id": cid, "event_id": event_id, "kind": "fact",
            "text": txt.strip()[:500], "source_item_id": item_id})

    # implication 是助手对我方的判断，按 §7 落为 recommendation：
    # 不得带 source_item_id —— 能追溯到某篇文章的就不是建议，是别人的观点
    imp = data.get("implication")
    if isinstance(imp, dict) and isinstance(imp.get("text"), str) \
            and len(imp["text"].strip()) >= 8:
        txt = imp["text"].strip()[:500]
        cid = "cl_" + hashlib.sha1(f"{event_id}r{txt}".encode()).hexdigest()[:12]
        store.upsert("claims", {
            "id": cid, "event_id": event_id, "kind": "recommendation",
            "text": txt, "source_item_id": None,
            "audience": imp.get("audience"),
            "needs_internal_data": 1 if imp.get("needs_internal_data") else 0})

    for inf in data.get("inferences") or []:
        txt = inf.get("text") if isinstance(inf, dict) else inf
        if not isinstance(txt, str) or len(txt.strip()) < 6:
            continue
        attr = (inf.get("attributed_to") if isinstance(inf, dict) else None) or "报道方"
        cid = "cl_" + hashlib.sha1(f"{event_id}{item_id}i{txt}".encode()).hexdigest()[:12]
        store.upsert("claims", {
            "id": cid, "event_id": event_id, "kind": "inference",
            "text": txt.strip()[:500], "source_item_id": item_id,
            "attributed_to": attr})


# ==================================================================== S5
def _org_key(url: str) -> str:
    """未登记信源的机构标识。

    早先直接用 url[:40] 当 key，同一主体的多篇稿件只要第 40 个字符之后不同
    就会被算成多个独立机构——实测 x.com/alibaba_cloud 的两条推文
    （/status/2095… 与 /status/2094…）把事件抬进了「多源已验证」，
    claude.com/blog 的两篇同理。这正是 org 去重本该拦下的「虚假多源」。

    改为按可注册域名归组；社交平台按「域名 + 账号」，因为同一平台上的
    不同账号确实是不同主体，一刀切按域名会把它们错误合并。
    """
    p = urllib.parse.urlsplit(url or "")
    host = (p.netloc or "").lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if not host:                      # 拿不到域名时退回旧行为，至少不误合并
        return "unregistered:" + (url or "")[:40]
    parts = host.split(".")
    if len(parts) > 2:
        keep = 3 if ".".join(parts[-2:]) in _MULTI_SUFFIX else 2
        host = ".".join(parts[-keep:])
    if host in _SOCIAL_HOSTS:
        seg = [s for s in p.path.split("/") if s]
        if seg:
            return f"unregistered:{host}/{seg[0].lower()}"
    return "unregistered:" + host


def stage_verify(store, cfg: dict, srccfg: dict) -> dict:
    """核验：纯规则，可回归、可审计。模型不参与可信度裁决。"""
    src_by_id = {s["id"]: s for s in srccfg["sources"]}
    window = cfg["report_window_days"]
    period_start = datetime.now(timezone.utc) - timedelta(days=window)
    obs_start = datetime.now(timezone.utc) - timedelta(days=cfg["window_days"])

    stats = {"events": 0, "官方确认·多源印证": 0, "官方一手": 0, "多源已验证": 0,
             "深度单源": 0, "单源待确认": 0, "pending_review": 0,
             "新增": 0, "延续": 0, "静默": 0, "旧闻/背景": 0,
             "stage_discounted": 0, "backdated": 0}

    for ev in store.q("SELECT * FROM events"):
        items = store.q("SELECT * FROM items WHERE event_id=?", (ev["id"],))
        if not items:
            continue
        stats["events"] += 1

        # ---------- 独立组织数（排除与事件主体关联的信源）----------
        orgs = set()
        has_official = has_deep = False
        for it in items:
            s = src_by_id.get(it["source_id"] or "")
            eff = it["effective_class"] or "D"
            if s and s.get("affiliated_with") == ev["company"]:
                if eff == "A":
                    has_official = True
                continue                       # 自家媒体不计独立信源
            if s:
                orgs.add(s["org"])
            else:
                orgs.add(_org_key(it["url"] or ""))
            if eff == "B":
                has_deep = True

        n_org = len(orgs)

        # ---------- 五级置信度（技术方案 §6.1）----------
        if has_official and n_org >= cfg["verify"]["min_independent_orgs"]:
            conf = "官方确认·多源印证"
        elif has_official:
            conf = "官方一手"
        elif n_org >= cfg["verify"]["min_independent_orgs"]:
            conf = "多源已验证"
        elif has_deep:
            conf = "深度单源"
        else:
            conf = "单源待确认"
        stats[conf] += 1

        # ---------- 阶段打折（技术方案 §6.2）----------
        official_stage = evidence_stage = None
        for it in items:
            st = it["draft_stage"]
            if st not in STAGE_ORDER:
                continue
            if (it["effective_class"] or "") == "A":
                official_stage = st if official_stage is None else \
                    min(official_stage, st, key=lambda x: STAGE_ORDER[x])
            else:
                evidence_stage = st if evidence_stage is None else \
                    max(evidence_stage, st, key=lambda x: STAGE_ORDER[x])

        if official_stage and evidence_stage:
            stage = min(official_stage, evidence_stage, key=lambda x: STAGE_ORDER[x])
            basis = f"官方称「{official_stage}」，第三方报道显示「{evidence_stage}」，取保守值"
            if stage != official_stage:
                stats["stage_discounted"] += 1
        elif official_stage:
            stage, basis = official_stage, "仅官方口径，未经第三方验证"
        elif evidence_stage:
            stage, basis = evidence_stage, "依据第三方报道"
        else:
            stage, basis = None, "信息不足以判断落地阶段"

        # ---------- 状态判定（技术方案 §6.4）----------
        first_seen = ev["first_seen_at"] or _now()
        edate = ev["event_date"]
        # 关键：event_date 二次窗口校验优先。报道可能是新的，但讲的是历史事件
        # ——首轮实跑抽出过「2023年618」「2025年双11」这类回顾性内容，
        # 若只看 first_seen_at 会全部误判为「新增」。
        if edate and not _after(edate, obs_start):
            status = "旧闻/背景"
            stats["backdated"] += 1
        elif _after(first_seen, period_start):
            status = "新增"
        elif any(_after(it["discovered_at"], period_start) for it in items):
            status = "延续"
        elif edate and _after(edate, obs_start):
            status = "静默"
        else:
            status = "旧闻/背景"
        stats[status] += 1

        # 人工复核结果必须黏住：只有从未复核过（none）的单源事件才进队列。
        # 早先无条件把单源事件置为 pending，导致每跑一轮流水线，
        # 人工裁决过的 watching/confirmed 全被冲掉、队列反复回到原点——
        # 人在环的意义就没了。
        prev = ev["review_state"] or "none"
        if prev in ("watching", "confirmed", "rejected", "suspect_duplicate"):
            review = prev
        else:
            review = "pending" if conf == "单源待确认" else "none"
        if review == "pending":
            stats["pending_review"] += 1

        store.db.execute("""UPDATE events SET confidence=?, independent_orgs=?,
                            stage=?, stage_basis=?, status=?, review_state=? WHERE id=?""",
                         (conf, n_org, stage, basis, status, review, ev["id"]))

    store.commit()
    return stats


def _after(ts: str | None, ref: datetime) -> bool:
    if not ts:
        return False
    try:
        s = ts if "T" in ts else ts + "T00:00:00+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d >= ref
    except ValueError:
        return False
