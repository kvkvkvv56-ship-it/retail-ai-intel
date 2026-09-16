"""一次性修复：把被后续报道推后的 event_date 拉回最早的那个值。

背景：S4a 的 upsert 是跨轮次的——同一件事隔几天又被报道、模型给出同一个
`event_key` 时走 INSERT OR REPLACE，而原实现只拿**本轮成员**算 `min(dates)`
写回，库里已有的日期被直接盖掉。实测两例：

  EV-5e07ed95  OpenAI 发布 GPT-6 Astra 旗舰模型   2026-09-03 → 2026-09-15
  EV-69a86407  AI内容强制标识新规落地             2025-09-01 → 2025-09-19

两个日期都比事情真正发生的时间晚了一大截——GPT-6 Astra 不是 09-15 发布的，
09-15 只是两篇解读文章进库的日子。代码侧已由 `process._anchor_date` 修好
（日期只能往前提，不能往后推），但已经写错的历史记录不会自己回来：
upsert 只在该 event_key 又抓到新条目时才触发，没有新报道的事件会一直错下去。

**为什么从 git 历史恢复，而不是按条目重算。** 事件日期是 S3 从正文里抽的
（「9 月 3 日发布」），落到 events 表之后，条目侧的原始抽取值就被事件 ID
覆盖了，库里找不回来。拿条目的 published_at 当替代会得到 09-11（首篇报道的
发布日），仍然不是 09-03。而 data/events.jsonl 每轮都随提交落盘，历史里存着
被覆盖前的真值——取**该事件在全部历史中出现过的最小日期**，正好等于修好后的
代码本该收敛到的结果。

只往前提，不往后推：历史最小值不会晚于当前值，所以这个脚本不可能把任何
事件的日期改晚。

用法：python3 scripts/fix_event_dates.py [--dry]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.store import ROOT, Store                 # noqa: E402

REL = "data/events.jsonl"


def history_min(dataset: str) -> dict[str, str]:
    """扫 git 历史里每一版 events.jsonl，取每个事件出现过的最早 event_date。"""
    rel = REL if dataset == "ecommerce" else f"data/{dataset}/events.jsonl"
    commits = subprocess.run(
        ["git", "log", "--format=%H", "--", rel],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    best: dict[str, str] = {}
    for c in commits:
        blob = subprocess.run(["git", "show", f"{c}:{rel}"],
                              cwd=ROOT, capture_output=True, text=True)
        if blob.returncode:
            continue
        for line in blob.stdout.splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            d = e.get("event_date")
            if d and (e["id"] not in best or d < best[e["id"]]):
                best[e["id"]] = d
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="ecommerce")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    store = Store(dataset=a.dataset)
    store.rebuild()
    best = history_min(a.dataset)
    print(f"git 历史覆盖 {len(best)} 个事件的日期")

    fixes = []
    for ev in store.q("SELECT id, title, event_date, first_seen_at FROM events"):
        old, hist = ev["event_date"], best.get(ev["id"])
        if old and hist and hist < old:
            fixes.append((ev["id"], old, hist, ev["title"]))

    if not fixes:
        print("✅ 没有被后推的 event_date")
        return

    print(f"\n日期被后推的事件 {len(fixes)} 个：")
    for eid, old, new, title in fixes:
        print(f"  {eid}  {old} → {new}   {title}")

    if a.dry:
        print("\n--dry：未写入")
        return

    for eid, _, new, _ in fixes:
        store.db.execute("UPDATE events SET event_date=? WHERE id=?", (new, eid))

    # S6 的 same_actor_track 边是按 event_date 排序取相邻两个建的，而且只增不改
    # （见 insight.stage_edges 的 existing 跳过逻辑）。用错日期排出来的相邻关系
    # 会一直留着：EV-5e07ed95 被挂在 09-15 的邻居上，而它实际发生在 09-03。
    # 删掉这几条，S6 下一轮会按修正后的日期补出正确的相邻边。
    # 只删 created_by='rule' 的：模型提议的语义边（follows/causes）不受日期影响。
    dropped = 0
    for eid, _, new, _ in fixes:
        for e in store.q("""SELECT from_event, to_event, basis FROM edges
                            WHERE relation='same_actor_track' AND created_by='rule'
                              AND (from_event=? OR to_event=?)""", (eid, eid)):
            if new in (e["basis"] or ""):
                continue                       # 建边时用的就是正确日期，留着
            store.db.execute("""DELETE FROM edges WHERE from_event=? AND to_event=?
                                AND relation='same_actor_track'""",
                             (e["from_event"], e["to_event"]))
            dropped += 1

    store.commit()
    counts = store.dump()
    store.close()
    print(f"\n✅ 已修正 {len(fixes)} 个事件的日期")
    print(f"   清掉按错误日期建的相邻边 {dropped} 条（S6 下一轮按新日期重建）")
    print(f"   回写 {counts['events']} 事件 / {counts['edges']} 关系边到 JSONL")
    print("   下一步：python3 -m pipeline.export  重新生成前端快照")


if __name__ == "__main__":
    main()
