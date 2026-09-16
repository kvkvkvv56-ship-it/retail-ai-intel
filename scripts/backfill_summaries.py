"""一次性回填：把被后续报道整体替换掉的事件标题/摘要，恢复成「首次出现 + 补充」。

背景：S4a 的跨轮次 upsert 原来每轮拿本轮成员的标题和摘要直接覆盖，同一件事
隔几天被另一家从别的角度转述，事件就被改名换面。代码侧已由
`process._anchor_text` + `_supplement_summaries` 修好（标题锚定首次出现，
新信息由模型合并进原摘要），但已经被覆盖的历史记录不会自己回来——补充只在
该事件又抓到新条目时才触发，没有后续报道的事件会一直停在被覆盖的版本上。

实测三例，丢掉的恰恰是事件最关键的信息：

    EV-5e07ed95  首版「OpenAI 于 9 月 3 日发布…Frontier Math Tier 4 得 97.6 分、
                 上下文窗口 105 万 token」→ 被一篇技术解读整体换掉，发布日期
                 和全部跑分一起消失
    EV-69a86407  首版「2025年9月1日，网信办等部门联合发布…」→ 生效日期与发布
                 主体消失
    EV-271e741b  首版「4月27日 HappyHorse 1.0 开启灰测」→ 被两个月后的盲测
                 榜单结果整体顶替

和日期一样，首版内容从 git 历史里取：events.jsonl 每轮随提交落盘，历史里
存着被覆盖前的标题和摘要。合并用的是流水线同一套提示词（P.SUPPLEMENT_SYS），
保证回填结果与今后每轮产出的口径一致。

**需要 KB_LLM_API_KEY**（摘要合并要调模型）。先跑 --dry 看清单。

用法：python3 scripts/backfill_summaries.py [--dry] [--keep-titles]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import prompts as P                        # noqa: E402
from pipeline.llm import LLM                             # noqa: E402
from pipeline.store import ROOT, Store                   # noqa: E402


def history(dataset: str) -> dict[str, list[tuple[str, str]]]:
    """每个事件在 git 历史里出现过的 (title, summary)，按时间先后去重保序。"""
    rel = "data/events.jsonl" if dataset == "ecommerce" \
        else f"data/{dataset}/events.jsonl"
    commits = subprocess.run(["git", "log", "--format=%H", "--reverse", "--", rel],
                             cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.split()
    seen: dict[str, list[tuple[str, str]]] = {}
    for c in commits:
        blob = subprocess.run(["git", "show", f"{c}:{rel}"],
                              cwd=ROOT, capture_output=True, text=True)
        if blob.returncode:
            continue
        for line in blob.stdout.splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            v = (e.get("title") or "", e.get("summary") or "")
            hist = seen.setdefault(e["id"], [])
            if not hist or hist[-1] != v:
                hist.append(v)
    return seen


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="ecommerce")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--keep-titles", action="store_true",
                    help="只合并摘要，不把标题恢复成首版")
    a = ap.parse_args()

    store = Store(dataset=a.dataset)
    store.rebuild()
    hist = history(a.dataset)

    todo = []
    for ev in store.q("SELECT id, title, summary FROM events"):
        versions = hist.get(ev["id"]) or []
        if len(versions) < 2:
            continue
        t0, s0 = versions[0]
        # 后续版本里新出现的摘要，就是当年被拿来覆盖的那些报道
        later = [s for _, s in versions[1:] if s and s != s0]
        if not later and (a.keep_titles or ev["title"] == t0):
            continue
        todo.append({"eid": ev["id"], "title": t0 or ev["title"],
                     "cur_title": ev["title"], "old": s0,
                     "cur": ev["summary"], "new": later})

    if not todo:
        print("✅ 没有被整体覆盖的标题/摘要")
        return

    print(f"被后续报道覆盖过的事件 {len(todo)} 个：\n")
    for t in todo:
        print(f"  {t['eid']}")
        if t["cur_title"] != t["title"]:
            print(f"    标题  现：{t['cur_title']}")
            print(f"          首：{t['title']}"
                  + ("   （--keep-titles，不改）" if a.keep_titles else "   → 恢复首版"))
        print(f"    首版摘要：{t['old'][:100]}")
        print(f"    现摘要：  {(t['cur'] or '')[:100]}")
        print(f"    待并入 {len(t['new'])} 条后续报道\n")

    if a.dry:
        print("--dry：未写入")
        return

    llm = LLM()
    done = fail = 0
    for t in todo:
        if not a.keep_titles and t["cur_title"] != t["title"]:
            store.db.execute("UPDATE events SET title=? WHERE id=?",
                             (t["title"], t["eid"]))
        if not t["new"]:
            continue
        try:
            res = llm.chat_json(
                [{"role": "system", "content": P.SUPPLEMENT_SYS},
                 {"role": "user", "content": P.supplement_user([t])}],
                strong=True, max_tokens=2200)
        except Exception as e:                                # noqa: BLE001
            print(f"  ⚠️  {t['eid']} 合并失败：{type(e).__name__}: {e}")
            fail += 1
            continue
        arr = res if isinstance(res, list) else (res.get("results") or [])
        merged = next((x.get("summary") for x in arr if isinstance(x, dict)), None)
        # 合并失败或模型判定无新信息 → 落回首版摘要。首版一定不比现状差：
        # 现状是首版被整体换掉的结果，本来就丢了信息
        final = merged.strip() if isinstance(merged, str) and merged.strip() \
            else t["old"]
        store.db.execute("UPDATE events SET summary=? WHERE id=?",
                         (final, t["eid"]))
        done += 1

    store.commit()
    counts = store.dump()
    store.close()
    print(f"\n✅ 回填 {done} 个事件的摘要" + (f"，{fail} 个失败" if fail else ""))
    print(f"   回写 {counts['events']} 条事件到 JSONL")
    print("   下一步：python3 -m pipeline.export  重新生成前端快照")


if __name__ == "__main__":
    main()
