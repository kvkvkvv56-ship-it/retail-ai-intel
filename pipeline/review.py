#!/usr/bin/env python3
"""人工复核 CLI（技术方案 §12）

三个 HITL 节点。流水线刻意保守——单源不自动采信、疑似重复不自动合并——
代价是留下需要人判断的队列。这里就是那个队列的处理入口。

    python3 pipeline/review.py queue                    # 看待办
    python3 pipeline/review.py event EV-xxxx            # 看单个事件的判断依据
    python3 pipeline/review.py event EV-xxxx confirm  --note "..."
    python3 pipeline/review.py event EV-xxxx reject   --note "..."
    python3 pipeline/review.py event EV-xxxx watch    --note "..."
    python3 pipeline/review.py merge EV-a EV-b split    # 判定为不同事件
    python3 pipeline/review.py merge EV-a EV-b same     # 判定为同一事件，合并
    python3 pipeline/review.py source <id> --class B --reliability 0.9
    python3 pipeline/review.py source <id> --inactive

工作流：git pull → 复核 → git push，下一轮流水线自动带上结果。
每次操作都写 reviews 表并同步 JSONL，复核记录会展示在线上。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.store import ROOT, Store                        # noqa: E402

REVIEWER = "operator"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(store, ttype, tid, action, note):
    store.insert("reviews", {
        "target_type": ttype, "target_id": tid, "action": action,
        "note": note, "reviewer": REVIEWER, "created_at": _now()})


# ==================================================================== queue
def cmd_queue(store, _):
    pend = store.q("""SELECT id, title, company, confidence, status, review_state,
                             independent_orgs, event_date
                      FROM events WHERE review_state IN ('pending','suspect_duplicate')
                      ORDER BY review_state, event_date DESC""")
    watching = store.one("SELECT COUNT(*) FROM events WHERE review_state='watching'")
    # watching 是「已裁决为持续观察」，不是待办
    print(f"\n待复核事件 {len(pend)} 个"
          + (f"（另有 {watching} 个已转持续观察，无需处理）" if watching else ""))
    print("=" * 92)
    by = {}
    for e in pend:
        by.setdefault(e["review_state"], []).append(e)

    label = {"pending": "① 单源待确认 —— 仅一个信源支撑，需人工判断是否采信",
             "suspect_duplicate": "② 疑似重复 —— 自动归并刻意保守未合并，需人工裁决"}
    for st, rows in by.items():
        print(f"\n{label.get(st, st)}  （{len(rows)} 个）")
        print("-" * 92)
        for e in rows:
            print(f"  {e['id']}  [{e['event_date'] or '日期未知'}] {e['company']:12} "
                  f"{e['title'][:38]}")

    # 标记记录里的事件可能在后续轮次被自动归并掉，留下陈旧引用。
    # 展示前校验存在性，只列仍可裁决的组。
    alive = {r["id"] for r in store.q("SELECT id FROM events")}
    done = {r["target_id"] for r in
            store.q("SELECT target_id FROM reviews WHERE action IN ('split','same')")}
    # 批量裁决同样算已处理：涉及的事件已脱离 suspect_duplicate 态，
    # 标记若还挂着会造成「队列清空但仍显示待裁决」的自相矛盾
    resolved = {r["id"] for r in store.q(
        "SELECT id FROM events WHERE review_state IN ('confirmed','rejected','watching')")}
    flags, stale = [], 0
    for f in store.q("SELECT target_id, note FROM reviews WHERE action='flag'"):
        ids = f["target_id"].split("|")
        if not all(i in alive for i in ids):
            stale += 1
            continue
        if f["target_id"] in done or "|".join(reversed(ids)) in done:
            continue
        if all(i in resolved for i in ids):
            continue
        flags.append(f)

    if flags:
        print(f"\n③ 系统标记的疑似重复对（{len(flags)} 组待裁决）")
        print("-" * 92)
        for f in flags:
            print(f"  {f['target_id']}")
            print(f"    {f['note'][:110]}")
    if stale:
        print(f"\n（另有 {stale} 组标记因其中的事件已被后续自动归并合掉而失效，已跳过）")
    print()


# ==================================================================== batch
def cmd_batch(store, a):
    """批量裁决同一类待办。

    一次批量操作是**一个人的一次判断**，所以只记一条 review，
    在 note 里写清适用范围与条数——而不是写 56 条一模一样的记录。
    逐条留痕看似严谨，实际是把有效信息淹没在重复里。
    """
    where = {"pending": "review_state='pending'",
             "suspect": "review_state='suspect_duplicate'",
             "single": "confidence='单源待确认'"}[a.scope]
    rows = store.q(f"SELECT id, title, confidence FROM events WHERE {where}")
    if not rows:
        print(f"范围 {a.scope} 下没有待处理事件")
        return 0

    state = {"confirm": "confirmed", "reject": "rejected", "watch": "watching"}[a.action]
    ids = [r["id"] for r in rows]
    store.db.execute(
        f"UPDATE events SET review_state='{state}' WHERE {where}")
    _log(store, "batch", f"{a.scope}×{len(ids)}", a.action,
         (a.note or "") + f"｜适用 {len(ids)} 个事件：" + "、".join(ids[:6])
         + (f" 等（共 {len(ids)} 个）" if len(ids) > 6 else ""))
    store.commit()
    print(f"✅ {len(ids)} 个事件 → {state}（记 1 条复核记录，非逐条）")
    for r in rows[:5]:
        print(f"     {r['id']}  {r['title'][:40]}")
    if len(rows) > 5:
        print(f"     …另 {len(rows)-5} 个")
    return 0


# ==================================================================== event
def cmd_event(store, a):
    e = store.q("SELECT * FROM events WHERE id=?", (a.id,))
    if not e:
        print(f"事件 {a.id} 不存在")
        return 1
    e = e[0]

    if not a.action:
        # 只看不改：把判断依据摊开
        print(f"\n{e['id']}  {e['title']}")
        print("=" * 92)
        print(f"  公司 {e['company']}   阶段 {e['stage'] or '未判定'}   "
              f"状态 {e['status']}   置信度 {e['confidence']}")
        print(f"  阶段依据：{e['stage_basis']}")
        print(f"  独立信源组织数：{e['independent_orgs']}   复核态：{e['review_state']}")
        print(f"\n  概述：{e['summary'] or '—'}")
        print("\n  事实：")
        for c in store.q("SELECT text FROM claims WHERE event_id=? AND kind='fact'", (a.id,)):
            print(f"    · {c['text'][:100]}")
        print("\n  信源：")
        for it in store.q("SELECT * FROM items WHERE event_id=?", (a.id,)):
            print(f"    [{it['effective_class']}] {it['source_id'] or '未登记'}  "
                  f"{(it['published_at'] or '')[:10]}  {it['title'][:52]}")
            print(f"        {it['url'][:88]}")
        print()
        return 0

    state = {"confirm": "confirmed", "reject": "rejected", "watch": "watching"}[a.action]
    store.db.execute("UPDATE events SET review_state=? WHERE id=?", (state, a.id))
    _log(store, "event", a.id, a.action, a.note or "")
    store.commit()
    print(f"✅ {a.id} → {state}"
          + (f"（{a.note}）" if a.note else ""))
    return 0


# ==================================================================== merge
def cmd_merge(store, a):
    ids = [a.a, a.b]
    evs = {r["id"]: dict(r) for r in
           store.q(f"SELECT * FROM events WHERE id IN (?,?)", tuple(ids))}
    missing = [i for i in ids if i not in evs]
    if missing:
        print(f"事件不存在：{missing}")
        return 1

    if a.action == "split":
        for i in ids:
            store.db.execute(
                "UPDATE events SET review_state='confirmed' WHERE id=?", (i,))
        _log(store, "merge", "|".join(ids), "split",
             a.note or "人工判定为不同事件，维持分开")
        store.commit()
        print(f"✅ 判定为不同事件，两者复核态置为 confirmed")
        return 0

    # same：合并，保留信源多的一方
    counts = {i: store.one("SELECT COUNT(*) FROM items WHERE event_id=?", (i,)) or 0
              for i in ids}
    keep, drop = (ids[0], ids[1]) if counts[ids[0]] >= counts[ids[1]] else (ids[1], ids[0])
    store.db.execute("UPDATE items  SET event_id=? WHERE event_id=?", (keep, drop))
    store.db.execute("UPDATE claims SET event_id=? WHERE event_id=?", (keep, drop))
    store.db.execute("DELETE FROM edges WHERE from_event=? OR to_event=?", (drop, drop))
    store.db.execute("DELETE FROM events WHERE id=?", (drop,))
    store.db.execute("UPDATE events SET review_state='confirmed' WHERE id=?", (keep,))
    store.insert("rejects", {
        "run_id": None, "item_id": None, "url": None,
        "title": evs[drop]["title"], "source_name": "—",
        "stage": "human_review", "reason_code": "merged",
        "reason_detail": f"人工复核判定与 {keep} 为同一事件。{a.note or ''}",
        "merged_into": keep, "created_at": _now()})
    _log(store, "merge", f"{keep}|{drop}", "same", a.note or "人工判定为同一事件")
    store.commit()
    print(f"✅ {drop} 已并入 {keep}（信源 {counts[drop]} → {counts[keep] + counts[drop]}）")
    return 0


# ==================================================================== source
def cmd_source(store, a):
    cfg_path = ROOT / "config" / f"sources.{a.dataset}.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    hit = next((s for s in cfg["sources"] if s["id"] == a.id), None)
    if not hit:
        print(f"信源 {a.id} 不在注册表中")
        return 1

    changes = []
    if a.cls:
        changes.append(f"source_class {hit['source_class']} → {a.cls}")
        hit["source_class"] = a.cls
    if a.reliability is not None:
        changes.append(f"reliability {hit.get('reliability')} → {a.reliability}")
        hit["reliability"] = a.reliability
    if a.inactive:
        changes.append("active → false")
        hit["active"] = False
    if a.activate:
        changes.append("active → true")
        hit["active"] = True
    if not changes:
        print(json.dumps(hit, ensure_ascii=False, indent=2))
        return 0

    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    store.upsert("sources", hit)
    _log(store, "source", a.id, "retier", "；".join(changes) + f"。{a.note or ''}")
    store.commit()
    print(f"✅ {hit['name']}：" + "；".join(changes))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="人工复核 CLI")
    ap.add_argument("--dataset", default="ecommerce")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("queue", help="列出待复核队列")

    pe = sub.add_parser("event", help="查看或裁决单个事件")
    pe.add_argument("id")
    pe.add_argument("action", nargs="?", choices=["confirm", "reject", "watch"])
    pe.add_argument("--note", default="")

    pb = sub.add_parser("batch", help="批量裁决同类待办（只记一条复核记录）")
    pb.add_argument("scope", choices=["pending", "suspect", "single"])
    pb.add_argument("action", choices=["confirm", "reject", "watch"])
    pb.add_argument("--note", default="")

    pm = sub.add_parser("merge", help="归并纠错")
    pm.add_argument("a"); pm.add_argument("b")
    pm.add_argument("action", choices=["split", "same"])
    pm.add_argument("--note", default="")

    ps = sub.add_parser("source", help="信源分级维护")
    ps.add_argument("id")
    ps.add_argument("--class", dest="cls", choices=list("ABCDE"))
    ps.add_argument("--reliability", type=float)
    ps.add_argument("--inactive", action="store_true")
    ps.add_argument("--activate", action="store_true")
    ps.add_argument("--note", default="")

    a = ap.parse_args()
    store = Store(dataset=a.dataset)
    try:
        rc = {"queue": cmd_queue, "event": cmd_event, "batch": cmd_batch,
              "merge": cmd_merge, "source": cmd_source}[a.cmd](store, a)
        if a.cmd != "queue":
            store.dump()
    finally:
        store.close()
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
