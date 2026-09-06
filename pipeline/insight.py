"""S6 关系边补全 · S7 洞察合成（周报）

关系边分两类来源：
  rule  —— 确定性规则生成（same_actor_track），零成本、可回归
  llm   —— S4 归并时模型提议的 follows/corroborates 等，必带 basis

周报是 **recommendation 唯一的产出点**。技术方案 §7 定死了硬边界：
建议不能有信源——能追溯到某篇文章的就不是建议，是别人的观点。
所以建议只能在这一步由助手基于全局事实生成，且必须带
audience（运营/商分/营销）与 needs_internal_data。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from pipeline.llm import parse_json

WEEKLY_SYS = """你是「行业与竞对 AI 洞察助手」的首席分析官，为零售业务团队产出情报洞察周报。

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
    stats = {"rule_edges": 0}
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


# ==================================================================== S7
def _period(cfg: dict) -> tuple[str, str]:
    """报告窗口：上一个完整自然周（周一~周日）。"""
    today = datetime.now(timezone.utc).date()
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(days=7)
    return start.isoformat(), (this_monday - timedelta(days=1)).isoformat()


def _prev_watchlist(store) -> list[str]:
    """取上一期 watchlist —— 技术方案 §9.1 要求喂进 prompt 做延续性检查。
    v1 设计了这个输入但从未真正传入，本实现补上。"""
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


def stage_insight(store, llm, cfg: dict, run_id: str) -> dict:
    """周报合成。输入本期事件清单 + 上期 watchlist。"""
    dom_name = {d["id"]: d["name"] for d in cfg["domains"]}
    comp_name = {c["id"]: c["name"] for c in cfg["companies"]}
    p_start, p_end = _period(cfg)

    # 本期事件 = 新增 + 延续（旧闻/背景不进周报正文，仅作历史上下文）
    evs = store.q("""SELECT * FROM events WHERE status IN ('新增','延续')
                     ORDER BY company, event_date DESC""")
    stats = {"period": f"{p_start}~{p_end}", "events_in": len(evs),
             "key_findings": 0, "implications": 0, "watchlist": 0, "error": None}
    if not evs:
        stats["error"] = "本期无新增或延续事件，跳过周报"
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

    prev = _prev_watchlist(store)
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

    rid = "W-" + p_start
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
