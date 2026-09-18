"""S6 关系边补全 · S6b 语义关系召回 · S7 洞察合成（周报）

关系边分三类来源：
  rule  —— 确定性规则生成（same_actor_track），零成本、可回归
  llm   —— S4 归并时模型在本轮、本公司候选内提议的 follows 等，必带 basis
  llm   —— S6b 向量召回跨轮次、跨公司的相近事件对，模型逐对判关系

周报是 **recommendation 唯一的产出点**。技术方案 §7 定死了硬边界：
建议不能有信源——能追溯到某篇文章的就不是建议，是别人的观点。
所以建议只能在这一步由助手基于全局事实生成，且必须带
audience（运营/商分/营销）与 needs_internal_data。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

from pipeline import embed as EMB
from pipeline import process as PR
from pipeline import prompts as P
from pipeline.llm import parse_json

WEEKLY_SYS = """你是「行业与竞对 AI 洞察情报站」的首席分析官，为零售业务团队产出情报洞察周报。

角色与边界：
- 只基于输入的事件清单判断，**不引入清单之外的信息**；单源事件支撑的结论必须标注置信边界
- 判断要跨公司、看格局与趋势，**不做单事件复述**；每条发现必须锚定证据事件（EV 编号，可多个）
- 语气专业克制、信息密度优先，不用营销话术和空洞展望
- 事实与推断分开表述：来自事件清单的是事实，你的分析是推断，推断要能追溯到具体 EV

输入：
- 本期事件清单（EV 编号 / 公司 / 领域 / 状态 / 置信度 / 落地阶段 / 事实要点）
- 上期观察清单（用于延续性检查——本期是否兑现、是否有新进展）

输出要求（只输出 JSON，不要任何其他内容）：
{
  "headline": "本期最重要的一条主线判断，≤40字，要有信息量，不要写成标题党",
  "key_findings": [
    {"title": "≤20字", "detail": "80-150字，跨公司或跨事件的判断，不复述单条事件",
     "evidence": ["EV-xxxxxxxx", ...]}
  ],
  "domain_summaries": {
    "retail_ops":          {"summary": "本期该领域态势，无事件则写「本期无观察到的动态」", "watch": "下期观察点"},
    "marketing":           {"summary": "...", "watch": "..."},
    "merchant_tools":      {"summary": "...", "watch": "..."},
    "data_bi":             {"summary": "...", "watch": "..."},
    "service_fulfillment": {"summary": "...", "watch": "..."}
  },
  "continuity": [
    {"prev_watch": "上期观察项原文", "status": "已兑现|有进展|无进展",
     "note": "依据，指向 EV 编号"}
  ],
  "implications": [
    {"text": "对我方的具体借鉴建议，要可执行，不要空泛",
     "audience": "运营|商分|营销",
     "needs_internal_data": true/false,
     "based_on": ["EV-xxxxxxxx", ...]}
  ],
  "watchlist": [
    {"text": "下期观察项，不要在文本里写 EV 编号", "evidence": ["EV-xxxxxxxx", ...]}
  ],
  "method_notes": "本期数据的可信度边界：哪些结论受单源限制、哪些领域本期信息不足、有哪些已知盲区。要具体，不要写套话。"
}

key_findings 数量按本期实际事件密度定，**不凑数**。事件少就少写。
implications 必须标注是否需要内部数据验证——公开信息无法验证的效果类判断一律标 true。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ==================================================================== S6
def stage_edges(store) -> dict:
    """规则关系边：同主体动作序列（same_actor_track）。

    同一公司的事件按时间排序，相邻两个建立边。这类边零成本、确定性，
    是叙事链的骨架；语义关系（follows/causes/contradicts）由 S4 的
    模型提议补充。
    """
    stats = {"rule_edges": 0, "pruned": 0}

    # 先清掉端点已不存在的边。
    #
    # 归并删事件时会顺手删掉挂在它身上的边，但另有两条路径会留下悬空边：
    # 早期 S4 直接拿模型给的 event_key 算 sha1 当端点、不校验事件是否存在
    # （现已在 S4 拦下），以及人工 unmerge 之后的边重挂。实测库里留了 2 条，
    # 占当时全部模型边的一半——前端拿不到标题，只能把 EV 编号打出来，
    # 点进去是空页；知识图谱里则是一条连向虚空的线。
    #
    # 导出层已经会过滤它们，但真相源该是干净的：JSONL 里躺着永远解析不了的
    # 边，下次谁再写一个消费方就要重新踩一遍。
    alive = {r["id"] for r in store.q("SELECT id FROM events")}
    dead = [r["id"] for r in store.q("SELECT id, from_event, to_event FROM edges")
            if r["from_event"] not in alive or r["to_event"] not in alive]
    for eid in dead:
        store.db.execute("DELETE FROM edges WHERE id=?", (eid,))
    stats["pruned"] = len(dead)

    existing = {(r["from_event"], r["to_event"], r["relation"])
                for r in store.q("SELECT from_event,to_event,relation FROM edges")}

    for c in store.q("SELECT DISTINCT company FROM events"):
        evs = store.q("""SELECT id, title, event_date FROM events
                         WHERE company=? AND event_date IS NOT NULL
                         ORDER BY event_date""", (c["company"],))
        for a, b in zip(evs, evs[1:]):
            k = (a["id"], b["id"], "same_actor_track")
            if k in existing:
                continue
            store.insert("edges", {
                "from_event": a["id"], "to_event": b["id"],
                "relation": "same_actor_track",
                "basis": f"同为 {c['company']} 的动作，时间相邻"
                         f"（{a['event_date']} → {b['event_date']}）",
                "created_by": "rule"})
            existing.add(k)
            stats["rule_edges"] += 1
    store.commit()
    return stats


# =================================================================== S6b
# 语义关系召回。**双向链接真正的内容来源**。
#
# 在此之前，库里 463 条边有 459 条是 S6 的规则边：同一公司的事件按 event_date
# 排序、相邻两个连一条。它给的是顺序不是关系，basis 是模板化的「时间相邻」，
# 前端自己都判定没有信息量不予展示（EventDetail.jsx 的注释写着这件事）。
# 剩下 4 条模型边只在 S4 内部产生——同一轮、同一公司的候选之间，
# 于是「上个月那件事的后续」永远不会被提出来。
#
# 本阶段补的就是这条通道：事件向量已经为 S4b 算好了，S4b 只用了 0.80 以上
# 那 1%，剩下的全部丢弃。而「像但不是同一件事」恰恰住在下面那一段里——
# 0.80 以上是重复（S4b 合掉），0.62 以下基本无关，中间这段就是关系候选。
# 边际成本接近零：向量是现成的，只多了模型逐对裁决那几次调用。
#
# 三道限流让每轮成本恒定，不随库容量增长：
#   focus  只问本轮动过的事件（新建或有新证据并入的）
#   topk   每个 focus 成员最多取 5 个邻居
#   账本   判过「无关」的对记进 reviews，下轮不再问第二遍
# focus 不足时用「还没有任何语义边的事件」补齐——存量会随轮次逐步长满，
# 而每轮问的对数始终在 min_focus × topk 这个量级。
RELATE = {"enabled": True, "min_cosine": 0.62, "max_cosine": 0.80,
          "topk": 5, "batch": 8, "min_focus": 12}

REL_KINDS = {"follows", "causes", "corroborates", "contradicts"}


def _pair_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


def stage_relate(store, llm, cfg: dict, run_id: str,
                 since: str | None = None) -> dict:
    """S6b 事件关系边：向量召回「相近但不同」的事件对 + LLM 逐对判关系。

    缺 JINA_API_KEY 时整段跳过——没有召回通道就只能整表通读，而事件两两组合
    在 289 事件上是 4 万对，那是塞不进提示词的。行为退回本阶段不存在时一致。
    """
    conf = {**RELATE, **(cfg.get("relate") or {})}
    stats = {"scanned": 0, "focus": 0, "recall_pairs": 0, "asked": 0,
             "edges": 0, "none": 0, "error": 0, "skipped": None}
    if not conf["enabled"]:
        stats["skipped"] = "配置关闭"
        return stats
    if not EMB.available():
        stats["skipped"] = "缺 JINA_API_KEY，跳过语义关系召回（图中只剩规则边）"
        return stats

    evs = store.q("""SELECT id, company, title, summary, event_date, last_update_at
                     FROM events""")
    stats["scanned"] = len(evs)
    if len(evs) < 2:
        return stats

    vecs = PR.event_vectors(store, evs)
    if vecs is None:
        stats["skipped"] = "事件向量不可用"
        return stats

    # ---------------------------------------------------------- focus 选取
    # 本轮动过的事件优先；不足则用「还没有任何语义边」的补齐，近期的先补。
    sem_deg: dict[str, int] = {}
    for r in store.q("""SELECT from_event, to_event FROM edges
                        WHERE created_by != 'rule'"""):
        sem_deg[r["from_event"]] = sem_deg.get(r["from_event"], 0) + 1
        sem_deg[r["to_event"]] = sem_deg.get(r["to_event"], 0) + 1

    focus = {i for i, e in enumerate(evs)
             if since and (e["last_update_at"] or "") >= since}
    if len(focus) < conf["min_focus"]:
        spare = sorted((i for i, e in enumerate(evs)
                        if i not in focus and not sem_deg.get(e["id"])),
                       key=lambda i: (evs[i]["event_date"] or "", evs[i]["id"]),
                       reverse=True)
        focus.update(spare[:conf["min_focus"] - len(focus)])
    stats["focus"] = len(focus)
    if not focus:
        return stats

    pairs = EMB.band_pairs(vecs, conf["min_cosine"], conf["max_cosine"],
                           focus=focus, top=conf["topk"])
    stats["recall_pairs"] = len(pairs)
    if not pairs:
        return stats

    # ------------------------------------------------------------ 已问过的
    # 语义边已存在的对不再问（规则边不算已回答——same_actor_track 覆盖了同公司
    # 所有相邻事件，把它当「已回答」会让同公司的 follows 永远问不出来）。
    answered = {_pair_key(r["from_event"], r["to_event"]) for r in store.q(
        "SELECT from_event, to_event FROM edges WHERE created_by != 'rule'")}
    answered |= {r["target_id"] for r in store.q(
        "SELECT target_id FROM reviews WHERE target_type='relate'")}

    todo = [(a, b, c) for a, b, c in pairs
            if _pair_key(evs[a]["id"], evs[b]["id"]) not in answered]
    stats["asked"] = len(todo)
    if not todo:
        return stats

    facts_of = {}

    def side(e) -> dict:
        if e["id"] not in facts_of:
            facts_of[e["id"]] = [f["text"][:90] for f in store.q(
                "SELECT text FROM claims WHERE event_id=? AND kind='fact' LIMIT 3",
                (e["id"],))]
        return {"id": e["id"], "company": e["company"], "title": e["title"],
                "date": e["event_date"], "facts": facts_of[e["id"]]}

    existing = {(r["from_event"], r["to_event"], r["relation"])
                for r in store.q("SELECT from_event,to_event,relation FROM edges")}

    for s0 in range(0, len(todo), conf["batch"]):
        batch = todo[s0:s0 + conf["batch"]]
        payload = [{"sim": c, "a": side(evs[a]), "b": side(evs[b])}
                   for a, b, c in batch]
        try:
            res = llm.chat_json([
                {"role": "system", "content": P.RELATE_SYS},
                {"role": "user", "content": P.relate_user(payload)},
            ], strong=True, max_tokens=1400)
        except Exception:                                     # noqa: BLE001
            stats["error"] += 1
            continue
        if isinstance(res, dict):
            res = res.get("pairs") or []
        verdicts = {v.get("i"): v for v in res if isinstance(v, dict)}

        for j, (a, b, c) in enumerate(batch):
            ea, eb = evs[a], evs[b]
            v = verdicts.get(j) or {}
            rel = (v.get("relation") or "none").strip()
            basis = (v.get("basis") or "").strip()

            # 模型没给具体依据的一律按 none 落账。RELATE_SYS 里写死了
            # 「写不出具体依据就判 none」，这里是程序层的同一条约束——
            # 无依据的边在图上和有依据的边长得一模一样，读图的人分不出来。
            if rel not in REL_KINDS or len(basis) < 8:
                store.insert("reviews", {
                    "target_type": "relate", "target_id": _pair_key(ea["id"], eb["id"]),
                    "action": "no_relation", "reviewer": "system",
                    "note": f"余弦 {c:.3f}，模型判定无值得记录的关系",
                    "created_at": _now()})
                stats["none"] += 1
                continue

            src, dst = (ea, eb) if str(v.get("from", "A")).upper() != "B" else (eb, ea)
            k = (src["id"], dst["id"], rel)
            if k in existing:
                continue
            existing.add(k)
            store.insert("edges", {
                "from_event": src["id"], "to_event": dst["id"], "relation": rel,
                "basis": f"{basis}（向量召回 {c:.2f}，模型判定）",
                "created_by": "llm"})
            stats["edges"] += 1

    store.commit()
    return stats


# ==================================================================== S7
def _period(cfg: dict) -> tuple[str, str]:
    """报告窗口：上一个完整自然周（周一~周日）。"""
    today = datetime.now(timezone.utc).date()
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(days=7)
    return start.isoformat(), (this_monday - timedelta(days=1)).isoformat()


def week_of(d: str) -> str:
    """某日期所属自然周的周一。"""
    y, m, dd = (int(x) for x in d[:10].split("-"))
    x = date(y, m, dd)
    return (x - timedelta(days=x.weekday())).isoformat()


def weeks_with_events(store, min_events: int = 3) -> list[str]:
    """有足够事件量、值得出周报的自然周，由早到晚。

    只有 1-2 个事件的周不出周报——周报的价值在跨事件看格局，
    单事件复述没有意义（周报 prompt 里也写了这条）。

    还没过完的当周一律不算：窗口没走完就出，拿到的是半周事实，结论会被
    后面几天的事件推翻。2026-09-07 的全量回填就是因为这里没拦住，给当时
    还剩六天的本周出了一份只有 5 条事件的周报。
    """
    this_week = week_of(datetime.now(timezone.utc).date().isoformat())
    counts: dict[str, int] = {}
    for r in store.q("SELECT event_date FROM events WHERE event_date IS NOT NULL"):
        counts[week_of(r["event_date"])] = counts.get(week_of(r["event_date"]), 0) + 1
    return sorted(w for w, n in counts.items() if n >= min_events and w < this_week)


def _prev_watchlist(store, before: str | None = None) -> list[str]:
    """取上一期 watchlist —— 技术方案 §9.1 要求喂进 prompt 做延续性检查。
    v1 设计了这个输入但从未真正传入，本实现补上。
    回填历史时按 before 取「该期之前最近的一期」，保证链条正确。"""
    if before:
        rows = store.q("""SELECT body FROM reports WHERE period_start < ?
                          ORDER BY period_start DESC LIMIT 1""", (before,))
    else:
        rows = store.q("SELECT body FROM reports ORDER BY period_start DESC LIMIT 1")
    if not rows:
        return []
    try:
        body = rows[0]["body"]
        body = json.loads(body) if isinstance(body, str) else body
        return [w.get("text", "") if isinstance(w, dict) else str(w)
                for w in (body.get("watchlist") or [])]
    except (json.JSONDecodeError, AttributeError):
        return []


def stage_insight(store, llm, cfg: dict, run_id: str,
                  period_start: str | None = None, force: bool = False) -> dict:
    """周报合成。输入本期事件清单 + 上期 watchlist。

    周期口径是**事件发生周**（event_date 落在该自然周内），不是「本轮观察到」。
    两者是不同的东西：前者回答「上周行业发生了什么」，是读者对周报的预期，
    也使历史回填成为可能；后者只反映系统何时发现，无法回溯。
    """
    dom_name = {d["id"]: d["name"] for d in cfg["domains"]}
    comp_name = {c["id"]: c["name"] for c in cfg["companies"]}

    if period_start:
        p_start = period_start
        y, m, dd = (int(x) for x in p_start.split("-"))
        p_end = (date(y, m, dd) + timedelta(days=6)).isoformat()
    else:
        p_start, p_end = _period(cfg)

    evs = store.q("""SELECT * FROM events
                     WHERE event_date >= ? AND event_date <= ?
                     ORDER BY company, event_date DESC""", (p_start, p_end))
    stats = {"period": f"{p_start}~{p_end}", "events_in": len(evs),
             "key_findings": 0, "implications": 0, "watchlist": 0, "error": None}

    # 一周只出一份。流水线每天跑两轮，若不设这道闸，同一周的周报会被反复
    # 重算并覆盖十几次——既白花模型钱，内容还每轮都在变，读者昨天看到的
    # 结论今天可能就没了。
    #
    # 判据用「该周的周报是否已存在」而不是「今天是不是周一」：
    # 效果同样是周一第一轮出上周报告，但多了自愈——某轮被 GitHub 的
    # schedule 丢掉、或那天模型调用失败，下一轮会自动补上，
    # 而按星期判断的话就永久错过了。
    #
    # 但「存在」本身不够，还要求这份是在窗口结束之后出的。2026-09-07 的全量
    # 回填把当时才过了半天的本周（09-07~09-13）也出了一份，输入只有周一上午
    # 的 5 条事件。一周之后的 09-14 周一，窗口第一次轮到这一周（实到 78 条
    # 事件），闸门看见记录存在就跳过了——那份 5 条事件的快照会就此成为终稿，
    # 本周后续 13 轮的判断完全相同，且日志只写「已生成」，看不出异常。
    # 加上生成时间判据后，窗口没结束就出的那份只当草稿，下一轮照常重出，
    # 仍然是自愈，不需要人工干预。
    rid = "W-" + p_start
    prev_gen = store.one("SELECT generated_at FROM reports WHERE id=?", (rid,))
    if not force and prev_gen and prev_gen[:10] > p_end:
        stats["skipped"] = (f"本周周报已生成（{prev_gen[:10]} 出），"
                            "跳过（--force-weekly 可重出）")
        return stats
    if prev_gen and prev_gen[:10] <= p_end:
        # 覆盖的是残周快照。日志里必须点名，否则和常规重出分不开
        stats["replaced"] = prev_gen[:10]

    if not evs:
        stats["error"] = f"{p_start}~{p_end} 无事件，跳过"
        return stats

    lines = []
    for e in evs:
        facts = store.q("""SELECT text FROM claims WHERE event_id=? AND kind='fact'
                           LIMIT 4""", (e["id"],))
        doms = e["domains"]
        doms = json.loads(doms) if isinstance(doms, str) and doms.startswith("[") else []
        lines.append(
            f"{e['id']} | {comp_name.get(e['company'], e['company'])} | "
            f"{'/'.join(dom_name.get(d, d) for d in doms) or '未分类'} | "
            f"{e['status']} | {e['confidence']} | 阶段：{e['stage'] or '未判定'}\n"
            f"  标题：{e['title']}\n"
            + "".join(f"  · {f['text'][:120]}\n" for f in facts))

    prev = _prev_watchlist(store, p_start)
    user = (f"本期事件清单（{len(evs)} 条，窗口 {p_start} ~ {p_end}）：\n\n"
            + "\n".join(lines)
            + "\n\n上期观察清单：\n"
            + ("\n".join(f"- {w}" for w in prev) if prev else "（首期，无上期观察清单）"))

    try:
        res = llm.chat_json(
            [{"role": "system", "content": WEEKLY_SYS},
             {"role": "user", "content": user}],
            strong=True, temperature=0.3, max_tokens=4000)
    except Exception as e:                                    # noqa: BLE001
        stats["error"] = f"{type(e).__name__}: {e}"
        return stats

    valid_ids = {e["id"] for e in evs}
    res = _sanitize(res, valid_ids)

    # 重出本期时先清掉上一版的建议——它们已不在任何一期周报里，
    # 留着只会让事件详情堆积无出处的建议
    store.db.execute("DELETE FROM claims WHERE kind='recommendation' AND attributed_to=?",
                     (rid,))
    store.upsert("reports", {
        "id": rid, "period_start": p_start, "period_end": p_end,
        "headline": res.get("headline", ""), "body": res,
        "generated_at": _now(), "run_id": run_id})

    # implications 落入 claims，受 §7 约束：建议不得有 source_item_id
    for imp in res.get("implications") or []:
        txt = imp.get("text", "")
        if len(txt) < 8:
            continue
        for eid in imp.get("based_on") or [None]:
            if eid not in valid_ids:
                continue
            cid = "cl_" + hashlib.sha1(f"{eid}r{txt}".encode()).hexdigest()[:12]
            store.upsert("claims", {
                "id": cid, "event_id": eid, "kind": "recommendation",
                "text": txt[:500], "source_item_id": None,
                # 标上产出它的周报编号。S3 的 implication 与这里用的是同一套
                # id 方案（sha1(event_id + "r" + text)），不打标就分不出来，
                # 周报重出时旧版建议会永远留在事件详情里
                "attributed_to": rid,
                "audience": imp.get("audience"),
                "needs_internal_data": 1 if imp.get("needs_internal_data") else 0})

    store.commit()
    stats.update({"key_findings": len(res.get("key_findings") or []),
                  "implications": len(res.get("implications") or []),
                  "watchlist": len(res.get("watchlist") or []),
                  "continuity": len(res.get("continuity") or []),
                  "headline": res.get("headline", "")})
    return stats


def _sanitize(res: dict, valid: set[str]) -> dict:
    """剔除模型编造的 EV 编号——只保留真实存在的事件引用。

    这是「关键结论需要有来源支撑」的程序层保障：引用不存在的证据，
    等同于没有证据。
    """
    def clean(ids):
        return [i for i in (ids or []) if i in valid]

    for f in res.get("key_findings") or []:
        f["evidence"] = clean(f.get("evidence"))
    res["key_findings"] = [f for f in (res.get("key_findings") or [])
                           if f.get("evidence")]

    for w in res.get("watchlist") or []:
        w["evidence"] = clean(w.get("evidence"))

    for imp in res.get("implications") or []:
        imp["based_on"] = clean(imp.get("based_on"))
    res["implications"] = [i for i in (res.get("implications") or [])
                           if i.get("based_on")]

    # 模型有时把 domain_summaries 的值写成字符串而非 {summary, watch}，
    # 前端按对象渲染会崩。这里统一归一化。
    ds = res.get("domain_summaries")
    if isinstance(ds, dict):
        res["domain_summaries"] = {
            k: (v if isinstance(v, dict)
                else {"summary": str(v), "watch": ""})
            for k, v in ds.items()}
    return res
