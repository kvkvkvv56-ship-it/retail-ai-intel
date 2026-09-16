"""S4a 跨轮次 upsert 的回归测试：事件以第一次出现为准，后续报道只补充。

为什么单独有这个文件：这类 bug 不会报错、不会掉数据量、CI 全绿，只是库里
的事件被悄悄改成了另一副样子——GPT-6 Astra 的发布日期被推后 12 天、首版
摘要里的跑分被整体换掉，都是跑了十几轮之后靠人眼看出来的。断言必须钉在
「跨轮次再 upsert 一次」这个具体动作上，才拦得住它重新长回来。

零第三方依赖，与流水线一致：

    python3 tests/test_event_anchor.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.process import stage_merge                 # noqa: E402
from pipeline.run import load_config                     # noqa: E402
from pipeline.store import Store                         # noqa: E402

KEY = "openai-gpt6-astra-release"
EID = "EV-5e07ed95"
FIRST_TITLE = "OpenAI 发布 GPT-6 Astra 旗舰模型"
FIRST_SUM = "OpenAI 于 9 月 3 日发布 GPT-6 Astra，Frontier Math Tier 4 得 97.6 分。"
MERGED = FIRST_SUM + "另有技术博主分析其循环 Transformer 设计。"

# 后一轮抓到的解读文章：标题更窄、摘要是另一个角度、领域标成了别的、日期是当天
LATER = {"event_key": KEY, "company": "ai_vendor",
         "event_title": "解读：循环 Transformer 与隐藏思维链",
         "summary": "技术博主 Sebastian Raschka 撰文分析循环 Transformer。",
         "domains": ["marketing"], "event_date": "2026-09-15",
         "facts": [], "inferences": [], "suggestions": [], "stage": "已上线"}


class StubLLM:
    """只允许被摘要合并调用。

    本轮只有一条候选，stage_merge 会走「无需归并」的短路分支，不该调归并；
    真调了说明分支逻辑被改坏了，这里直接断言拦住。
    """

    def __init__(self, behaviour: str = "merge"):
        self.behaviour = behaviour
        self.calls = 0

    def chat_json(self, messages, **kw):
        self.calls += 1
        assert "摘要维护员" in messages[0]["content"], "本轮仅一条候选，不该调用归并"
        if self.behaviour == "fail":
            raise RuntimeError("模拟接口失败")
        if self.behaviour == "null":                # 模型判定没带来新信息
            return {"results": [{"i": 0, "summary": None}]}
        return {"results": [{"i": 0, "summary": MERGED}]}


def upsert_again(tmp: Path, tag: str, *, first_title=FIRST_TITLE,
                 first_summary=FIRST_SUM, behaviour="merge") -> dict:
    """建一个 09-11 就入库的事件，再喂一条 09-15 的同 event_key 报道。"""
    cfg, _ = load_config("ecommerce")
    store = Store(db_path=tmp / f"{tag}.db", dataset="ecommerce")
    store.upsert("events", {
        "id": EID, "event_key": KEY, "company": "ai_vendor",
        "title": first_title, "summary": first_summary,
        "domains": ["ai_supply"], "event_date": "2026-09-03",
        "first_seen_at": "2026-09-11T16:41:22+00:00",
        "last_update_at": "2026-09-11T16:41:22+00:00"})
    store.upsert("items", {
        "id": "IT-later", "url": "https://example.test/a",
        "url_canonical": "https://example.test/a",
        "title": "万字详解 GPT-6 Astra", "content": "...", "source_id": "s1",
        "status": "extracted", "published_at": "2026-09-15T02:17:10+00:00",
        "discovered_at": "2026-09-15T05:06:18+00:00",
        "event_id": "draft:" + json.dumps(LATER, ensure_ascii=False)})
    store.commit()

    stats = stage_merge(store, StubLLM(behaviour), cfg, "RUN-test")
    r = store.q("SELECT * FROM events WHERE id=?", (EID,))[0]
    got = {"title": r["title"], "summary": r["summary"],
           "event_date": r["event_date"], "domains": json.loads(r["domains"]),
           "first_seen": r["first_seen_at"][:10],
           "supplemented": stats.get("summary_supplemented"),
           "failed": stats.get("summary_merge_failed", 0)}
    store.close()
    return got


def main() -> int:
    failures = 0

    def check(name: str, got: dict, **expect):
        nonlocal failures
        bad = {k: (got[k], v) for k, v in expect.items() if got[k] != v}
        print(("✅ " if not bad else "❌ ") + name)
        for k, (g, e) in bad.items():
            print(f"      {k}\n        实际 {g!r}\n        期望 {e!r}")
        failures += bool(bad)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        r = upsert_again(tmp, "normal")
        check("标题锚定首版，不被后一轮的窄标题改掉", r, title=FIRST_TITLE)
        check("摘要是合并结果，首版内容完整保留", r, summary=MERGED, supplemented=1)
        check("domains 累加，首版的 ai_supply 没被抹掉",
              r, domains=["ai_supply", "marketing"])
        check("event_date 只能往前提：09-15 的报道推不动 09-03",
              r, event_date="2026-09-03")
        check("first_seen_at 始终是第一次入库的时间", r, first_seen="2026-09-11")

        r = upsert_again(tmp, "llm_fail", behaviour="fail")
        check("合并调用失败 → 原样保留首版，不退回成后一轮的版本",
              r, summary=FIRST_SUM, title=FIRST_TITLE, supplemented=0, failed=1)

        r = upsert_again(tmp, "no_news", behaviour="null")
        check("模型判定无新信息（null）→ 保留首版摘要",
              r, summary=FIRST_SUM, supplemented=0)

        r = upsert_again(tmp, "degraded_title", first_title=KEY)
        check("首版标题降级成了 event_key → 允许后续补上（填空不是覆盖）",
              r, title=LATER["event_title"])

        r = upsert_again(tmp, "no_summary", first_summary=None)
        check("首版没有摘要 → 后一轮的摘要直接成为锚，不白花一次合并调用",
              r, summary=LATER["summary"], supplemented=0)

    print("\n全部通过" if not failures else f"\n{failures} 项未通过")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
