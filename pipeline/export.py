"""S8 导出：SQLite → 前端 API 快照

技术方案 §2.1：生产侧与消费侧分离。线上是只读快照，不挂数据库。

数据 1.4MB 已超 Worker 脚本体积上限，故快照落为**静态资产**，
由 Worker 按 §15 的 API 契约路由读取（env.ASSETS.fetch），
这样既无脚本体积限制，又保留真实的 API 契约与错误规范。

产物目录 web/public/api/v1/ ——路径即 API 路径，便于本地直接验证。
"""
from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from pipeline.store import ROOT, Store

OUT = ROOT / "web" / "public" / "api" / "v1"


def _w(rel: str, obj) -> int:
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    p.write_text(txt, encoding="utf-8")
    return len(txt)


def _j(v):
    """还原 JSONL/SQLite 中以字符串存放的 JSON 字段。"""
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def export(store: Store, cfg: dict, srccfg: dict) -> dict:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)

    src_by_id = {s["id"]: s for s in srccfg["sources"]}
    stats: dict[str, int] = {}
    total = 0

    # ---------------------------------------------------------------- meta
    runs = [dict(r) for r in store.q("SELECT * FROM runs ORDER BY started_at DESC")]
    for r in runs:
        r["stats"] = _j(r["stats"])
        r["cost"] = _j(r["cost"])

    counts = {t: store.one(f"SELECT COUNT(*) FROM {t}") for t in
              ("items", "events", "claims", "edges", "rejects", "sources",
               "reviews", "reports")}

    total += _w("meta.json", {
        "dataset": cfg["dataset"], "name": cfg["name"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": cfg["window_days"],
        "report_window_days": cfg["report_window_days"],
        "companies": cfg["companies"], "domains": cfg["domains"],
        "stages": cfg["stages"], "statuses": cfg["statuses"],
        "confidence_levels": cfg["confidence_levels"],
        "counts": counts,
        "latest_run": runs[0]["id"] if runs else None,
    })

    # -------------------------------------------------------------- events
    ev_rows = [dict(r) for r in store.q(
        "SELECT * FROM events ORDER BY COALESCE(event_date,'') DESC, id")]
    n_fact = dict(store.db.execute(
        "SELECT event_id, COUNT(*) FROM claims WHERE kind='fact' GROUP BY event_id"))
    n_infer = dict(store.db.execute(
        "SELECT event_id, COUNT(*) FROM claims WHERE kind='inference' GROUP BY event_id"))
    n_item = dict(store.db.execute(
        "SELECT event_id, COUNT(*) FROM items WHERE event_id IS NOT NULL GROUP BY event_id"))

    listing = []
    for e in ev_rows:
        e["domains"] = _j(e["domains"]) or []
        listing.append({
            "id": e["id"], "title": e["title"], "summary": e["summary"],
            "company": e["company"], "domains": e["domains"],
            "stage": e["stage"], "confidence": e["confidence"],
            "status": e["status"], "event_date": e["event_date"],
            "independent_orgs": e["independent_orgs"],
            "review_state": e["review_state"],
            "n_facts": n_fact.get(e["id"], 0),
            "n_inferences": n_infer.get(e["id"], 0),
            "n_sources": n_item.get(e["id"], 0),
        })
    total += _w("events.json", {"total": len(listing), "results": listing})
    stats["events"] = len(listing)

    # 事件详情：claims 三色分组 + 全部信源 + 关系边
    for e in ev_rows:
        claims = defaultdict(list)
        for c in store.q("SELECT * FROM claims WHERE event_id=? ORDER BY kind, id",
                         (e["id"],)):
            claims[c["kind"]].append({
                "id": c["id"], "text": c["text"],
                "source_item_id": c["source_item_id"],
                "attributed_to": c["attributed_to"],
                "audience": c["audience"],
                "needs_internal_data": bool(c["needs_internal_data"]),
            })
        items = []
        for it in store.q("SELECT * FROM items WHERE event_id=? ORDER BY published_at",
                          (e["id"],)):
            s = src_by_id.get(it["source_id"] or "")
            items.append({
                "id": it["id"], "title": it["title"], "url": it["url"],
                "source_id": it["source_id"],
                "source_name": s["name"] if s else "未登记域名",
                "source_class": it["source_class"],
                "effective_class": it["effective_class"],
                "original_source": it["original_source"],
                "partial_content": bool(s.get("partial_content")) if s else False,
                "published_at": it["published_at"], "time_source": it["time_source"],
                "discovered_at": it["discovered_at"], "channel": it["channel"],
                "excerpt": (it["content"] or "")[:400],
            })
        # 关联事件要带上对方的标题与日期——只给 EV 编号读者无从判断值不值得点。
        # 同时按 (对方, 关系) 去重：规则边与模型边可能指向同一对。
        ev_meta = {r["id"]: r for r in store.q(
            "SELECT id, title, company, event_date FROM events")}
        edges, seen_edge = [], set()
        for r in store.q("SELECT * FROM edges WHERE from_event=? OR to_event=?",
                         (e["id"], e["id"])):
            other = r["to_event"] if r["from_event"] == e["id"] else r["from_event"]
            if other == e["id"]:
                continue
            key = (other, r["relation"])
            if key in seen_edge:
                continue
            seen_edge.add(key)
            m = ev_meta.get(other)
            edges.append({
                "other": other, "relation": r["relation"], "basis": r["basis"],
                "created_by": r["created_by"],
                "direction": "out" if r["from_event"] == e["id"] else "in",
                "title": m["title"] if m else None,
                "company": m["company"] if m else None,
                "event_date": m["event_date"] if m else None,
            })
        edges.sort(key=lambda x: (x["relation"] != "follows", x["event_date"] or ""))
        total += _w(f"events/{e['id']}.json", {
            **e, "facts": claims["fact"], "inferences": claims["inference"],
            "recommendations": claims["recommendation"],
            "items": items, "edges": edges})

    # ---------------------------------------------------------------- runs
    total += _w("runs.json", {"total": len(runs), "results": runs})
    for r in runs:
        rejects = []
        for x in store.q("""SELECT * FROM rejects WHERE run_id=?
                            ORDER BY stage, reason_code""", (r["id"],)):
            rejects.append({k: x[k] for k in
                            ("stage", "reason_code", "reason_detail", "title",
                             "url", "source_name", "merged_into")})
        by_reason: dict[str, int] = defaultdict(int)
        for x in rejects:
            by_reason[x["reason_code"]] += 1
        total += _w(f"runs/{r['id']}.json", {**r, "reject_summary": dict(by_reason)})
        total += _w(f"runs/{r['id']}/rejects.json",
                    {"total": len(rejects), "by_reason": dict(by_reason),
                     "results": rejects})
    stats["runs"] = len(runs)

    # ------------------------------------------------------------- reports
    reps = [dict(r) for r in store.q(
        "SELECT * FROM reports ORDER BY period_start DESC")]
    total += _w("reports.json", {"total": len(reps), "results": [
        {k: r[k] for k in ("id", "period_start", "period_end", "headline",
                           "generated_at")} for r in reps]})
    for r in reps:
        total += _w(f"reports/{r['id']}.json", {**r, "body": _j(r["body"])})
    stats["reports"] = len(reps)

    # ------------------------------------------------- sources 信源健康度
    hit = dict(store.db.execute(
        "SELECT source_id, COUNT(*) FROM items GROUP BY source_id"))
    acc = dict(store.db.execute(
        "SELECT source_id, COUNT(*) FROM items WHERE status='accepted' GROUP BY source_id"))
    last = dict(store.db.execute(
        "SELECT source_id, MAX(discovered_at) FROM items GROUP BY source_id"))
    rej = dict(store.db.execute(
        "SELECT source_name, COUNT(*) FROM rejects GROUP BY source_name"))

    health = []
    for s in srccfg["sources"]:
        h, a = hit.get(s["id"], 0), acc.get(s["id"], 0)
        health.append({
            "id": s["id"], "name": s["name"], "org": s["org"],
            "source_class": s["source_class"], "channel": s["channel"],
            "affiliated_with": s.get("affiliated_with"),
            "reliability": s.get("reliability", 1.0),
            "active": s.get("active", True), "note": s.get("note"),
            "hits": h, "accepted": a,
            "accept_rate": round(a / h, 3) if h else None,
            "rejected": rej.get(s["name"], 0),
            "last_hit": last.get(s["id"]),
        })
    health.sort(key=lambda x: (-x["hits"], x["source_class"]))
    total += _w("sources.json", {
        "total": len(health),
        "by_channel": {c: sum(1 for x in health if x["channel"] == c)
                       for c in ("rss", "rsshub", "direct", "exa")},
        "results": health})
    stats["sources"] = len(health)

    # -------------------------------------------------- reviews 人工复核记录
    # 复核记录按操作类型聚合再外显。逐条列出会把有效信息淹没在重复里
    # ——尤其批量裁决之后。系统自动标记（reviewer=system）也不进人工记录。
    revs = [dict(r) for r in store.q(
        "SELECT * FROM reviews WHERE reviewer!='system' ORDER BY created_at DESC")]
    by_action: dict[str, int] = defaultdict(int)
    for r in revs:
        by_action[r["action"]] += 1
    affected = store.one(
        "SELECT COUNT(*) FROM events WHERE review_state NOT IN ('none','')") or 0
    total += _w("reviews.json", {
        "total": len(revs),
        "by_action": dict(by_action),
        "affected_events": affected,
        "results": revs[:12],          # 只给最近 12 条，其余靠聚合数交代
        "truncated": max(0, len(revs) - 12),
    })
    stats["reviews"] = len(revs)

    # ---------------------------------------------- 检索索引（前端全文检索）
    idx = [{"id": e["id"], "t": e["title"], "s": (e["summary"] or "")[:120],
            "c": e["company"], "d": e["domains"]} for e in ev_rows]
    total += _w("search-index.json", idx)

    # ------------------------------------------ 降级数据（技术方案 §10.6）
    # 构建时嵌入 JS bundle。API 取不到时页面仍可读，显示「离线模式」。
    # 只放读首页与列表所需的最小集，详情页仍需在线——完整数据 350KB，
    # 全塞进 bundle 会拖慢首屏，得不偿失。
    fallback = {
        "meta": json.loads((OUT / "meta.json").read_text(encoding="utf-8")),
        "events": {"total": len(listing), "results": listing},
        "runs": {"total": len(runs), "results": runs[:3]},
        "latest_run": json.loads((OUT / f"runs/{runs[0]['id']}.json").read_text(
            encoding="utf-8")) if runs else None,
        "reports": {"total": len(reps), "results": [
            {k: r[k] for k in ("id", "period_start", "period_end", "headline",
                               "generated_at")} for r in reps]},
        "latest_report": ({**reps[0], "body": _j(reps[0]["body"])} if reps else None),
    }
    fb = ROOT / "web" / "src" / "data"
    fb.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(fallback, ensure_ascii=False, separators=(",", ":"))
    (fb / "fallback.json").write_text(txt, encoding="utf-8")
    stats["fallback_kb"] = round(len(txt) / 1024)

    stats["bytes"] = total
    return stats


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    from pipeline.run import load_config
    cfg, srccfg = load_config("ecommerce")
    st = Store()
    r = export(st, cfg, srccfg)
    st.close()
    print("已导出 API 快照 → web/public/api/v1/")
    print("  " + "  ".join(f"{k}={v}" for k, v in r.items() if k != "bytes"))
    print(f"  总体积 {r['bytes'] / 1024:.0f} KB")
