"""S2 预筛 · S3 抽取 · S4 归并 · S5 核验

S2/S3/S4 用 LLM，S5 是纯规则。
分工原则（技术方案 §0）：**模型只做抽取，可信度由规则裁决。**
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from pipeline import prompts as P
from pipeline.llm import LLMError

STAGE_ORDER = {"概念宣传": 0, "试点探索": 1, "已上线": 2, "规模化应用": 3}
CLASS_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

            # 抽取结果暂存在 items 上，S4 归并后才建 event
            store.db.execute(
                "UPDATE items SET status='extracted', event_id=? WHERE id=?",
                ("draft:" + json.dumps(res, ensure_ascii=False), it["id"]))
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
    alias = {"1688": "taobao", "alibaba": "taobao", "aliyun": "taobao",
             "tmall": "taobao", "taotian": "taobao", "ali": "taobao",
             "bytedance": "douyin", "doubao": "douyin", "jinritemai": "douyin",
             "kwai": "kuaishou", "xhs": "xiaohongshu", "rednote": "xiaohongshu"}
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
                stats["error"] += 1
                continue

            for g in res.get("groups", []):
                members = [cands[i] for i in g.get("members", [])
                           if isinstance(i, int) and 0 <= i < len(cands)]
                if not members:
                    continue
                key = g.get("canonical_key") or members[0]["event_key"]
                eid = "EV-" + hashlib.sha1(f"{company}:{key}".encode()).hexdigest()[:8]

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

            for rel in res.get("relations", []):
                a = "EV-" + hashlib.sha1(f"{company}:{rel.get('from')}".encode()).hexdigest()[:8]
                b = "EV-" + hashlib.sha1(f"{company}:{rel.get('to')}".encode()).hexdigest()[:8]
                if a != b and rel.get("basis"):
                    store.insert("edges", {
                        "from_event": a, "to_event": b,
                        "relation": rel.get("relation") or "follows",
                        "basis": rel["basis"], "created_by": "llm"})
                    stats["relations"] += 1

    store.commit()
    return stats


def stage_merge_cross(store, llm, run_id: str) -> dict:
    """S4b 跨公司归并复核。

    S4 按公司分组归并有一个结构性盲区：同一事件若被抽取成不同 company，
    两者永远不会被放在一起比较。首轮实跑就命中了——「千问AI Arena」被判为
    taobao、「阿里云通义AI竞技场」被判为 industry，实为同一件事。
    （讽刺的是，这个漏归并是周报的 method_notes 自己发现并报告的。）

    本阶段对全部事件做一次整体扫描，事件量级为百级，一次调用即可。
    """
    evs = store.q("SELECT id, company, event_key, title, summary FROM events")
    stats = {"scanned": len(evs), "cross_merged": 0, "suspects": 0, "error": 0}
    if len(evs) < 2:
        return stats

    # 只给标题不够：实测「千问AI Arena」与「阿里云通义AI竞技场」标题词重合度
    # 仅 0.088，产品名完全不同（千问/通义、Arena/竞技场），必须给摘要与事实
    # 才能判断是同一件事。周报的 method_notes 能发现这个漏归并，正是因为它
    # 拿到了事实要点。
    lines = []
    for i, e in enumerate(evs):
        facts = store.q("""SELECT text FROM claims WHERE event_id=?
                           AND kind='fact' LIMIT 2""", (e["id"],))
        lines.append(
            f'[{i}] {e["company"]} | {e["event_key"]}\n'
            f'    标题：{e["title"]}\n'
            f'    概述：{(e["summary"] or "")[:100]}\n'
            + "".join(f'    · {f["text"][:90]}\n' for f in facts))
    try:
        res = llm.chat_json([
            {"role": "system", "content": P.MERGE_CROSS_SYS},
            {"role": "user", "content": "全部事件：\n\n" + "\n\n".join(lines)},
        ], strong=True, max_tokens=1500)
    except Exception:                                         # noqa: BLE001
        stats["error"] = 1
        return stats

    for g in res.get("duplicates") or []:
        idx = [i for i in g.get("members", [])
               if isinstance(i, int) and 0 <= i < len(evs)]
        if len(idx) < 2:
            continue
        members = [evs[i] for i in idx]
        # 保留信源最多的那个作为主事件
        counts = {m["id"]: store.one(
            "SELECT COUNT(*) FROM items WHERE event_id=?", (m["id"],)) or 0
            for m in members}
        keep = max(members, key=lambda m: counts[m["id"]])
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
            store.insert("rejects", {
                "run_id": run_id, "item_id": None, "url": None,
                "title": m["title"], "source_name": "—",
                "stage": "merge_cross", "reason_code": "merged",
                "reason_detail": f"跨公司复核：与 {keep['id']} 为同一事件。"
                                 f"{g.get('reason', '')}",
                "merged_into": keep["id"], "created_at": _now()})
            stats["cross_merged"] += 1
    # 模型表达的不确定性 → 人工复核队列（不自动合并）
    for g in res.get("suspects") or []:
        idx = [i for i in g.get("members", [])
               if isinstance(i, int) and 0 <= i < len(evs)]
        if len(idx) < 2:
            continue
        ms = [evs[i] for i in idx]
        for m in ms:
            store.db.execute(
                "UPDATE events SET review_state='suspect_duplicate' "
                "WHERE id=? AND review_state IN ('none','')", (m["id"],))
        store.insert("reviews", {
            "target_type": "merge", "target_id": "|".join(m["id"] for m in ms),
            "action": "flag",
            "note": "模型标记疑似重复，证据不足未自动合并：" + g.get("reason", "")
                    + "｜涉及：" + " / ".join(f'{m["title"][:26]}({m["company"]})'
                                              for m in ms),
            "reviewer": "system", "created_at": _now()})
        stats["suspects"] += 1

    stats["suspects"] += _flag_suspect_duplicates(store)
    store.commit()
    return stats


def _flag_suspect_duplicates(store, jaccard: float = 0.34) -> int:
    """标记疑似重复事件，进人工复核队列。

    S4b 的提示词要求「宁可漏掉也不要误合并」——误合并会不可逆地毁掉信息，
    漏合并只是冗余且可修。所以自动归并刻意保守，代价是会留下漏网。

    此处用零成本的标题词重合度做疑似标记，不自动合并，只挂进人工队列。
    这是 HITL「归并纠错」节点（技术方案 §12）的输入来源。
    """
    import re
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
                store.insert("reviews", {
                    "target_type": "merge", "target_id": f"{a['id']}|{b['id']}",
                    "action": "flag",
                    "note": f"疑似重复待人工判定：「{a['title'][:30]}」({a['company']}) "
                            f"与「{b['title'][:30]}」({b['company']}) 标题词高度重合，"
                            f"自动归并未合并（保守策略）",
                    "reviewer": "system", "created_at": _now()})
                n += 1
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
        items = store.q("""SELECT i.*, c.text AS ctext FROM items i
                           LEFT JOIN claims c ON 0 GROUP BY i.id
                           HAVING i.event_id=?""", (ev["id"],))
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
                orgs.add("unregistered:" + (it["url"] or "")[:40])
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
            d = _draft_of(store, it["id"])
            st = (d or {}).get("stage")
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

        review = "pending" if conf == "单源待确认" else (ev["review_state"] or "none")
        if review == "pending":
            stats["pending_review"] += 1

        store.db.execute("""UPDATE events SET confidence=?, independent_orgs=?,
                            stage=?, stage_basis=?, status=?, review_state=? WHERE id=?""",
                         (conf, n_org, stage, basis, status, review, ev["id"]))

    store.commit()
    return stats


def _draft_of(store, item_id: str) -> dict | None:
    """抽取草稿在归并前暂存于 items.event_id，归并后被事件 ID 覆盖。
    此处从 claims 反推不可行，故 S3 的 stage 单独缓存在内存表。"""
    return _DRAFTS.get(item_id)


_DRAFTS: dict[str, dict] = {}


def cache_drafts(store) -> None:
    """在 S4 覆盖 event_id 之前，把抽取草稿缓存起来供 S5 使用。"""
    for r in store.q("SELECT id, event_id FROM items WHERE event_id LIKE 'draft:%'"):
        try:
            _DRAFTS[r["id"]] = json.loads(r["event_id"][6:])
        except json.JSONDecodeError:
            continue


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
