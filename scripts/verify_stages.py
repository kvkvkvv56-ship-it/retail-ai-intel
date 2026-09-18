"""向量相关阶段的行为验证：S1b 语义去重 · S6b 关系召回 · S4/S6 的边端点。

    python3 scripts/verify_stages.py

**不需要任何 API key**，与 `run.py --mock` 的取向一致：
embed 层换成确定性假实现（向量由一个角度决定，两向量余弦 = cos(角差)），
LLM 换成按顺序吐预设答案的假对象。这样能把指定的几对精确推进召回区间，
验证的是阶段逻辑本身——召回 → 过滤 → 精判 → 决策 → 记账这条链是否成立，
而不是 Jina 或 DeepSeek 某次返回了什么。

跑在 intel.db 的**副本**上（每个用例一份独立副本），且绝不调用 dump()，
真数据与 data/*.jsonl 都不受影响。需要先有 intel.db：
    python3 pipeline/run.py --rebuild
"""
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SCRATCH = Path(tempfile.mkdtemp(prefix="verify-stages-"))

from pipeline import embed as EMB          # noqa: E402
from pipeline import insight as IN         # noqa: E402
from pipeline import process as PR         # noqa: E402
from pipeline.run import load_config       # noqa: E402
from pipeline.store import Store           # noqa: E402

DIM = 64
FAIL = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}{('  — ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


# ----------------------------------------------------------------- 假向量层
def make_vec(group: float) -> list[int]:
    """把标量 group 映射到单位圆上的一个方向，再撑到 DIM 维。
    两个向量的余弦 = cos(角差)，于是 group 差 0 → 1.0，差越大余弦越低。"""
    a = group
    v = [math.cos(a), math.sin(a)] + [0.0] * (DIM - 2)
    return [int(round(x * 127)) for x in v]


GROUPS: dict[str, float] = {}      # 文本 → 角度


FAR = 2.60          # 与测试里两个 focus 角度的余弦分别是 -0.86 / -0.23，稳在区间外


def fake_embed(texts):
    return [make_vec(GROUPS.get(t, FAR)) for t in texts]


class FakeLLM:
    """按顺序吐预设答案，并记录收到的提示词。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []

    def chat_json(self, messages, **kw):
        self.seen.append(messages[-1]["content"])
        if not self.answers:
            raise RuntimeError("假模型答案用尽")
        return self.answers.pop(0)


_N = [0]


def fresh_store():
    """每次都用一个新文件名：上一个 Store 可能还开着同名文件，
    直接覆盖会让它读到被改写过的页，测试之间互相串数据。"""
    _N[0] += 1
    src, dst = ROOT / "intel.db", SCRATCH / f"test{_N[0]}.db"
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.exists():
            shutil.copy(s, str(dst) + suffix)
    return Store(db_path=dst)


# ==================================================================== S1b
def test_s1b():
    print("\nS1b 条目层语义去重")
    st = fresh_store()
    cfg, _ = load_config("ecommerce")

    # 三条新条目：A 与 B 近乎同稿（应被判转载），C 独立
    rows = [
        ("it_testA", "淘宝上线 AI 生意管家新商版", "淘宝今日宣布上线 AI 生意管家新商版，面向新商家。", "2026-09-18T01:00:00+00:00"),
        ("it_testB", "淘宝 AI 生意管家新商版正式发布", "淘宝今日宣布上线 AI 生意管家新商版，面向新商家。", "2026-09-18T02:00:00+00:00"),
        ("it_testC", "美团闪购接入大模型做履约调度", "美团宣布闪购履约调度接入自研大模型。", "2026-09-18T03:00:00+00:00"),
    ]
    for i, (iid, title, content, disc) in enumerate(rows):
        st.upsert("items", {
            "id": iid, "url": f"https://t.example/{i}", "url_canonical": f"https://t.example/{i}",
            "title": title, "content": content, "source_id": None, "source_class": "C",
            "effective_class": "C", "published_at": disc, "discovered_at": disc,
            "run_id": "RUN-TEST", "channel": "rss", "status": "pending"})
    st.commit()

    GROUPS.clear()
    GROUPS[PR._l3_text(rows[0][1], rows[0][2])] = 0.00
    GROUPS[PR._l3_text(rows[1][1], rows[1][2])] = 0.02      # 与 A 余弦 0.9998
    GROUPS[PR._l3_text(rows[2][1], rows[2][2])] = 1.20      # 与 A 余弦 0.36

    EMB.available = lambda: True
    EMB.embed = fake_embed

    llm = FakeLLM([[{"i": 0, "dup": True, "why": "同一篇通稿"}]])
    out = PR.stage_dedup_semantic(st, llm, cfg, "RUN-TEST")
    print("   ", json.dumps(out, ensure_ascii=False))

    check("召回到 A/B 这一对", out["recall_pairs"] >= 1, f"recall_pairs={out['recall_pairs']}")
    check("判为转载 1 条", out["dup"] == 1)
    check("提示词带上了正文", "AI 生意管家新商版" in llm.seen[0])
    b = st.q("SELECT status FROM items WHERE id='it_testB'")[0]["status"]
    check("较晚发现的 B 被置为 rejected", b == "rejected", f"status={b}")
    a = st.q("SELECT status FROM items WHERE id='it_testA'")[0]["status"]
    check("较早的 A 保持 pending", a == "pending", f"status={a}")
    c = st.q("SELECT status FROM items WHERE id='it_testC'")[0]["status"]
    check("不相关的 C 未受影响", c == "pending", f"status={c}")
    rj = st.q("""SELECT * FROM rejects WHERE stage='semantic_dedup' AND item_id='it_testB'""")
    check("台账记了一条且指向 A", len(rj) == 1 and rj[0]["merged_into"] == "it_testA")
    check("台账写进了相似度", "余弦 " in (rj[0]["reason_detail"] if rj else ""),
          rj[0]["reason_detail"][:70] if rj else "")

    # 模型判「不是转载」时必须放行
    st2 = fresh_store()
    for i, (iid, title, content, disc) in enumerate(rows):
        st2.upsert("items", {
            "id": iid, "url": f"https://t.example/{i}", "url_canonical": f"https://t.example/{i}",
            "title": title, "content": content, "source_id": None, "source_class": "C",
            "effective_class": "C", "published_at": disc, "discovered_at": disc,
            "run_id": "RUN-TEST", "channel": "rss", "status": "pending"})
    st2.commit()
    llm2 = FakeLLM([[{"i": 0, "dup": False, "why": "两家独立采写"}]])
    out2 = PR.stage_dedup_semantic(st2, llm2, cfg, "RUN-TEST")
    check("判 false 时两条都留下（多源印证不能被吃掉）",
          out2["dup"] == 0 and out2["kept"] >= 1, json.dumps(out2, ensure_ascii=False))
    st.close(); st2.close()


# ==================================================================== S6b
def test_s6b():
    print("\nS6b 语义关系召回")
    cfg, _ = load_config("ecommerce")
    cfg = {**cfg, "relate": {**cfg["relate"], "min_focus": 2}}   # 关掉补位，只测单对
    st = fresh_store()

    evs = st.q("SELECT id, title, summary FROM events ORDER BY id LIMIT 60")
    ids = [e["id"] for e in evs]
    since = "2026-09-18T00:00:00+00:00"
    # 让前两个事件是本轮动过的 focus，且彼此余弦 0.70（落在 [0.62, 0.80)）
    st.db.execute("UPDATE events SET last_update_at=? WHERE id IN (?,?)",
                  (since, ids[0], ids[1]))
    st.db.execute("UPDATE events SET last_update_at='2000-01-01T00:00:00+00:00' "
                  "WHERE id NOT IN (?,?)", (ids[0], ids[1]))
    st.commit()

    angles = {ids[0]: 0.0, ids[1]: math.acos(0.70)}
    for k, i in enumerate(ids[2:], start=2):
        # 必须同时远离两个 focus：1.45 那档与 ids[1](0.795) 的余弦是 0.79，
        # 正好落在召回区间里，会平白多召回一堆对
        angles[i] = FAR + k * 0.01

    evs_all = st.q("SELECT id, company, event_key, title, summary, first_seen_at, event_date FROM events")
    by_id = {e["id"]: e for e in evs_all}

    GROUPS.clear()
    for i, ang in angles.items():
        GROUPS[PR._event_vec_text(st, by_id[i])] = ang

    EMB.available = lambda: True
    EMB.embed = fake_embed

    llm = FakeLLM([{"pairs": [{"i": 0, "relation": "follows", "from": "B",
                               "basis": "B 的产品发布在前，A 披露的是它上线后的用量数据"}]}])
    out = IN.stage_relate(st, llm, cfg, "RUN-TEST", since)
    print("   ", json.dumps(out, ensure_ascii=False))

    check("focus 就是本轮动过的两个事件", out["focus"] == 2, f"focus={out['focus']}")
    check("召回到 1 对", out["recall_pairs"] == 1, f"recall_pairs={out['recall_pairs']}")
    check("建了 1 条语义边", out["edges"] == 1)
    e = st.q("""SELECT * FROM edges WHERE created_by='llm'
                AND (from_event=? OR to_event=?) AND relation='follows'
                ORDER BY id DESC LIMIT 1""", (ids[1], ids[1]))
    check("方向按 from=B 落成 B → A",
          bool(e) and e[0]["from_event"] == ids[1] and e[0]["to_event"] == ids[0],
          f"{e[0]['from_event']}→{e[0]['to_event']}" if e else "无边")
    check("basis 里带上了召回相似度", bool(e) and "向量召回 0.70" in e[0]["basis"],
          e[0]["basis"][-24:] if e else "")

    # 同一对不会被问第二遍
    llm2 = FakeLLM([])
    out2 = IN.stage_relate(st, llm2, cfg, "RUN-TEST", since)
    check("已有语义边的对不再重复提问", out2["asked"] == 0, json.dumps(out2, ensure_ascii=False))

    # 判 none 时不建边，但要记账，下轮不再问
    st3 = fresh_store()
    st3.db.execute("UPDATE events SET last_update_at=? WHERE id IN (?,?)", (since, ids[0], ids[1]))
    st3.db.execute("UPDATE events SET last_update_at='2000-01-01T00:00:00+00:00' "
                   "WHERE id NOT IN (?,?)", (ids[0], ids[1]))
    st3.db.execute("DELETE FROM edges WHERE created_by='llm'")
    st3.commit()
    llm3 = FakeLLM([{"pairs": [{"i": 0, "relation": "none", "basis": ""}]}])
    out3 = IN.stage_relate(st3, llm3, cfg, "RUN-TEST", since)
    check("判 none 不建边", out3["edges"] == 0 and out3["none"] == 1,
          json.dumps(out3, ensure_ascii=False))
    r = st3.q("SELECT * FROM reviews WHERE target_type='relate'")
    check("none 记进 reviews 台账", len(r) == 1 and r[0]["action"] == "no_relation")
    check("台账里的 target_id 是排序过的无序对",
          bool(r) and r[0]["target_id"] == "|".join(sorted((ids[0], ids[1]))))
    llm4 = FakeLLM([])
    out4 = IN.stage_relate(st3, llm4, cfg, "RUN-TEST", since)
    check("判过 none 的对下一轮不再问", out4["asked"] == 0, json.dumps(out4, ensure_ascii=False))

    # 没给具体依据的一律按 none 落账（程序层兜住提示词里的硬约束）
    st5 = fresh_store()
    st5.db.execute("UPDATE events SET last_update_at=? WHERE id IN (?,?)", (since, ids[0], ids[1]))
    st5.db.execute("UPDATE events SET last_update_at='2000-01-01T00:00:00+00:00' "
                   "WHERE id NOT IN (?,?)", (ids[0], ids[1]))
    st5.db.execute("DELETE FROM edges WHERE created_by='llm'")
    st5.commit()
    llm5 = FakeLLM([{"pairs": [{"i": 0, "relation": "follows", "from": "A", "basis": "相关"}]}])
    out5 = IN.stage_relate(st5, llm5, cfg, "RUN-TEST", since)
    check("basis 太短的边被拒绝、按 none 落账",
          out5["edges"] == 0 and out5["none"] == 1, json.dumps(out5, ensure_ascii=False))
    st.close(); st3.close(); st5.close()


# ============================================================== S4 边端点
def test_s4_edge_endpoints():
    print("\nS4 关系边端点校验")
    st = fresh_store()
    cfg, _ = load_config("ecommerce")
    before = st.one("SELECT COUNT(*) FROM edges")
    live0 = {r["id"] for r in st.q("SELECT id FROM events")}
    dang0 = sum(1 for r in st.q("SELECT from_event,to_event FROM edges")
                if r["from_event"] not in live0 or r["to_event"] not in live0)

    # 两条候选（只有一条时 stage_merge 会短路、不调模型），
    # 模型在 relations 里一条引用真 key、一条引用编造的 key
    st.db.execute("UPDATE items SET status='pending'")
    ids = [r["id"] for r in st.q("SELECT id FROM items LIMIT 2")]
    for n, iid in enumerate(ids):
        st.db.execute("UPDATE items SET status='extracted', event_id=? WHERE id=?",
                      ('draft:' + json.dumps({"company": "alibaba", "event_key": f"k-{n}",
                                              "event_title": f"事件{n}", "summary": "s",
                                              "facts": ["一个足够长的事实"], "inferences": []},
                                             ensure_ascii=False), iid))
    st.commit()
    llm = FakeLLM([{"groups": [{"canonical_key": "k-0", "title": "事件0", "members": [0],
                                "reason": "独立"},
                               {"canonical_key": "k-1", "title": "事件1", "members": [1],
                                "reason": "独立"}],
                    "relations": [{"from": "k-0", "to": "k-1", "relation": "follows",
                                   "basis": "真实端点，应当建边"},
                                  {"from": "k-0", "to": "根本不存在的key",
                                   "relation": "follows", "basis": "编的端点，应当丢弃"}]}])
    out = PR.stage_merge(st, llm, cfg, "RUN-TEST")
    print("   ", json.dumps({k: v for k, v in out.items() if k != "companies"}, ensure_ascii=False))
    check("真实端点的边建成了", out.get("relations", 0) == 1)
    check("编造端点的边被丢弃", out.get("relations_dropped", 0) == 1)
    alive = {r["id"] for r in st.q("SELECT id FROM events")}
    dangling = [r for r in st.q("SELECT from_event,to_event FROM edges")
                if r["from_event"] not in alive or r["to_event"] not in alive]
    check("本阶段没有新增悬空边", len(dangling) == dang0,
          f"跑之前 {dang0} 条，跑之后 {len(dangling)} 条")
    check("边数只涨了 1（那条真实的）",
          st.one("SELECT COUNT(*) FROM edges") == before + 1,
          f"{before} → {st.one('SELECT COUNT(*) FROM edges')}")
    st.close()


# ============================================================== S6 悬空边清理
def test_prune():
    print("\nS6 悬空边清理")
    st = fresh_store()
    alive = {r["id"] for r in st.q("SELECT id FROM events")}
    # 先人为塞一条悬空边，确保这个用例在已经干净的库上也真的验到了东西
    st.insert("edges", {"from_event": "EV-nonexistent", "to_event": next(iter(alive)),
                        "relation": "follows", "basis": "测试用的悬空边",
                        "created_by": "llm"})
    st.commit()
    planted = [r for r in st.q("SELECT from_event,to_event FROM edges")
               if r["from_event"] not in alive or r["to_event"] not in alive]
    out = IN.stage_edges(st)
    after = [r for r in st.q("SELECT from_event,to_event FROM edges")
             if r["from_event"] not in alive or r["to_event"] not in alive]
    print("   ", json.dumps(out, ensure_ascii=False))
    check("悬空边被清掉", out["pruned"] == len(planted) and len(after) == 0,
          f"清理前 {len(planted)} 条，清理后 {len(after)} 条")
    st.close()


if __name__ == "__main__":
    if not (ROOT / "intel.db").exists():
        print("intel.db 不存在，请先跑：python3 pipeline/run.py --rebuild")
        sys.exit(2)
    test_s1b()
    test_s6b()
    test_s4_edge_endpoints()
    test_prune()
    shutil.rmtree(SCRATCH, ignore_errors=True)
    print("\n" + ("❌ 失败 %d 项：%s" % (len(FAIL), "、".join(FAIL)) if FAIL else "✅ 全部通过"))
    sys.exit(1 if FAIL else 0)
