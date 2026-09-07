"""一次性回填：为历史 items 补 draft_stage。

背景：S3 抽取出的 stage 原本只暂存在 items.event_id 的 draft 前缀里，
S4 归并时被事件 ID 覆盖，S5 再从内存字典读——于是只有「本轮新建的事件」
能拿到阶段，老事件每轮都被重算成「信息不足以判断落地阶段」。
修好落列逻辑后，历史条目的草稿已经丢失，只能重跑一遍阶段判定补上。

判定口径与 S3 一致：如实记录**这条报道所描述的**阶段，不做打折——
官方口径与第三方报道的交叉打折是 S5 的规则职责，不在这里做。

用法：python3 scripts/backfill_stage.py [--limit N] [--dry]
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.llm import LLM                      # noqa: E402
from pipeline.process import STAGE_ORDER          # noqa: E402
from pipeline.store import Store                  # noqa: E402

SYS = """你是信息抽取员。给你若干条电商/AI 领域的报道，判断每条报道描述的动作
处在哪个落地阶段。

阶段定义（四选一）：
- 概念宣传：只有愿景、口号、战略表态，没有具体产品或动作
- 试点探索：小范围内测、灰度、与个别商家合作试验
- 已上线：功能正式对外开放，商家/用户可用
- 规模化应用：给出了覆盖规模、渗透率、GMV 等量化结果

判定口径：
1. **如实记录这条报道所描述的阶段**，不要打折。官方常把试点说成上线，
   这个偏差由后续规则交叉修正，不是你的职责。
2. 报道信息不足以判断的，输出 null——宁可留空，不要猜。
3. 只看这条报道本身写了什么，不要用你对该公司的背景知识补全。

输出 JSON，不要任何其他内容：
{"results": [{"i": 序号, "stage": "阶段名或 null"}, ...]}
每条输入都必须有对应输出。"""


def batch(llm: LLM, rows: list) -> dict[str, str | None]:
    lines = []
    for n, r in enumerate(rows):
        body = (r["content"] or "")[:420].replace("\n", " ")
        lines.append(f"[{n}] 标题：{r['title']}\n     正文：{body}")
    res = llm.chat_json(
        [{"role": "system", "content": SYS},
         {"role": "user", "content": "\n\n".join(lines)}],
        strong=False, temperature=0, max_tokens=1200)
    # 模型偶尔直接返回裸数组而不是 {"results": [...]}，两种都收
    arr = res if isinstance(res, list) else (res.get("results") or [])
    out: dict[str, str | None] = {}
    for x in arr:
        if not isinstance(x, dict):
            continue
        try:
            r = rows[int(x["i"])]
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        st = x.get("stage")
        out[r["id"]] = st if st in STAGE_ORDER else None
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--size", type=int, default=10)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    store = Store()
    rows = store.q("""SELECT id, title, content FROM items
                      WHERE draft_stage IS NULL AND event_id IS NOT NULL
                        AND event_id NOT LIKE 'draft:%'
                      ORDER BY id""")
    if a.limit:
        rows = rows[:a.limit]
    print(f"待回填 {len(rows)} 条")
    if a.dry or not rows:
        return

    chunks = [rows[i:i + a.size] for i in range(0, len(rows), a.size)]
    llm = LLM()
    done = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(batch, llm, c): c for c in chunks}
        for n, f in enumerate(as_completed(futs), 1):
            try:
                done.update(f.result())
            except Exception as e:                             # noqa: BLE001
                print(f"  批次失败：{type(e).__name__}: {e}")
            print(f"\r  {n}/{len(chunks)} 批", end="", flush=True)
    print()

    hit = 0
    for iid, st in done.items():
        if st:
            store.db.execute("UPDATE items SET draft_stage=? WHERE id=?", (st, iid))
            hit += 1
    store.commit()

    dist = {r["draft_stage"]: r["n"] for r in store.q(
        "SELECT draft_stage, COUNT(*) n FROM items GROUP BY 1")}
    print(f"✅ 判出阶段 {hit} 条 / 覆盖 {len(done)} 条")
    print(f"   分布：{json.dumps(dist, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
