#!/usr/bin/env python3
"""流水线入口

    python3 pipeline/run.py                  # 完整一轮
    python3 pipeline/run.py --stage collect  # 只跑采集
    python3 pipeline/run.py --rebuild        # 从 JSONL 重建 intel.db
    python3 pipeline/run.py --no-exa         # 跳过 Exa（零成本，只跑免费通道）

阶段（docs/技术方案.md §2.1）：
    S0 采集 → S1 去重 → S2 预筛 → S3 抽取 → S4 归并 → S5 核验 → [S6 洞察]
    方括号内为 D7 待实现
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import collect as C                                  # noqa: E402
from pipeline import insight as IN                                 # noqa: E402
from pipeline import process as PR                                 # noqa: E402
from pipeline.llm import LLM                                       # noqa: E402
from pipeline.store import ROOT, Store                             # noqa: E402

CONFIG_DIR = ROOT / "config"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(dataset: str) -> tuple[dict, dict]:
    cfg = json.loads((CONFIG_DIR / f"{dataset}.json").read_text(encoding="utf-8"))
    src = json.loads((CONFIG_DIR / f"sources.{dataset}.json").read_text(encoding="utf-8"))
    return cfg, src


def config_hash(cfg: dict, src: dict) -> str:
    blob = json.dumps([cfg, src], ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha1(blob).hexdigest()[:12]


# ==================================================================== S0
def stage_collect(cfg: dict, srccfg: dict, use_exa: bool) -> tuple[list[dict], dict, list[str]]:
    sources = [s for s in srccfg["sources"] if s.get("active", True)]
    instances = srccfg["rsshub_instances"]
    raw, errors = [], []

    by_channel = {"rss": [], "rsshub": [], "direct": [], "jina": [],
                  "aihot": [], "exa": []}
    for s in sources:
        by_channel.setdefault(s["channel"], []).append(s)

    def run_source(s: dict):
        try:
            if s["channel"] == "rss":
                items, err = C.collect_rss(s, cfg)
            elif s["channel"] == "rsshub":
                items, err = C.collect_rsshub(s, cfg, instances)
            elif s["channel"] == "direct":
                items, err = C.collect_direct(s, cfg)
            elif s["channel"] == "jina":
                items, err = C.collect_jina(s, cfg)
            elif s["channel"] == "aihot":
                items, err = C.collect_aihot(s, cfg)
            else:
                return [], None
            for it in items:
                it["_src"] = s
                it["_channel"] = s["channel"]
            return items, err
        except Exception as e:                                # noqa: BLE001
            return [], f"{s['id']}: {type(e).__name__}: {e}"

    feed_sources = (by_channel["rss"] + by_channel["rsshub"]
                    + by_channel["direct"] + by_channel["jina"]
                    + by_channel["aihot"])
    print(f"  直采通道：{len(feed_sources)} 个信源 "
          f"(rss {len(by_channel['rss'])} / rsshub {len(by_channel['rsshub'])} "
          f"/ direct {len(by_channel['direct'])} / jina {len(by_channel['jina'])}"
          f" / aihot {len(by_channel['aihot'])})")

    with ThreadPoolExecutor(max_workers=6) as ex:
        for f in as_completed([ex.submit(run_source, s) for s in feed_sources]):
            items, err = f.result()
            raw.extend(items)
            if err:
                errors.append(err)

    cost = {"total": 0.0, "queries": 0}
    if use_exa:
        print(f"  Exa 通道：{len(cfg['search']['matrix'])} 组矩阵查询 "
              f"+ {len(cfg['search'].get('site_queries', []))} 组站内定向")
        exa_items, cost, exa_err = C.collect_exa(
            cfg, cfg.get("collect_window_days", cfg["window_days"]))
        for it in exa_items:
            it["_src"] = None
            it["_channel"] = "exa"
        raw.extend(exa_items)
        errors.extend(exa_err)
    else:
        print("  Exa 通道：已跳过（--no-exa）")

    return raw, cost, errors


# ==================================================================== S1
def stage_dedup(store: Store, raw: list[dict], cfg: dict, srccfg: dict,
                run_id: str) -> dict:
    """三层去重 + 拒绝台账（技术方案 §5）。

    L1 URL 精确  → duplicate_url
    L2 SimHash   → syndication（转载）
    L3 语义归并  → D5 实现（需 embedding + LLM 裁决）
    另有时间窗口与信源类别过滤。
    """
    domain_idx = C.build_domain_index(srccfg["sources"])
    patterns = srccfg.get("_original_source_patterns", {})
    patterns = {k: v for k, v in patterns.items() if not k.startswith("_")}
    src_by_id = {s["id"]: s for s in srccfg["sources"]}

    # 采集窗口 ≠ 观察窗口：前者决定往回捞多久，后者决定状态判定。
    # 早先混用一个参数，2026 上半年的事件在采集阶段就被判 stale 丢弃，
    # 而它们本该作为「旧闻/背景」入库提供历史上下文。
    collect_days = cfg.get("collect_window_days", cfg["window_days"])
    window_start = datetime.now(timezone.utc) - timedelta(days=collect_days)
    thr = cfg["verify"]["simhash_hamming_threshold"]

    known_urls = {r["url_canonical"] for r in store.q("SELECT url_canonical FROM items")}
    known_hashes = [(r["id"], r["simhash"]) for r in
                    store.q("SELECT id, simhash FROM items WHERE simhash IS NOT NULL")]
    known_titles = {C.normalize_title(r["title"]): r["id"]
                    for r in store.q("SELECT id, title FROM items")}

    rel = cfg.get("relevance", {})
    ai_terms = rel.get("ai_terms", [])
    ind_terms = rel.get("industry_terms", [])

    stats = {"raw": len(raw), "duplicate_url": 0, "syndication": 0, "off_topic": 0,
             "stale": 0, "no_title": 0, "accepted": 0, "attributed": 0}

    def reject(it, stage, code, detail, merged=None):
        store.insert("rejects", {
            "run_id": run_id, "item_id": None, "url": it.get("url"),
            "title": (it.get("title") or "")[:200],
            "source_name": (it.get("_src") or {}).get("name") or "未登记",
            "stage": stage, "reason_code": code, "reason_detail": detail,
            "merged_into": merged, "created_at": now_iso(),
        })

    batch_hashes: list[tuple[str, str]] = []

    for it in raw:
        title = (it.get("title") or "").strip()
        if not title or not it.get("url"):
            stats["no_title"] += 1
            reject(it, "collect", "no_title", "缺少标题或链接，无法追溯")
            continue

        # jina 通道的标题级粗筛结果：不抓全文，但必须留痕可审计
        if it.get("_prefilter_drop"):
            stats["off_topic"] += 1
            reject(it, "jina_title_filter", "off_topic", it["_prefilter_drop"])
            continue

        canon = C.canonical_url(it["url"])
        iid = C.item_id(canon)

        # ---- L1 URL 精确去重 ----
        if canon in known_urls:
            stats["duplicate_url"] += 1
            reject(it, "url_dedup", "duplicate_url", "该 URL 此前已采集，跳过（零 LLM 成本）")
            continue

        # ---- 时间窗口 ----
        pub, tsrc = it.get("published_at"), it.get("time_source")
        if pub and tsrc in ("exact", "parsed"):
            try:
                if datetime.fromisoformat(pub) < window_start:
                    stats["stale"] += 1
                    reject(it, "window", "stale",
                           f"发布于 {pub[:10]}，早于 {collect_days} 天采集窗口")
                    continue
            except ValueError:
                pass

        # ---- 关键词粗筛（零成本，LLM 预筛前）----
        blob = f"{title} {it.get('content') or ''}"
        if ai_terms and ind_terms:
            hit_ai = next((t for t in ai_terms if t in blob), None)
            hit_ind = next((t for t in ind_terms if t in blob), None)
            if not (hit_ai and hit_ind):
                stats["off_topic"] += 1
                miss = "AI 相关词" if not hit_ai else "行业相关词"
                reject(it, "keyword_filter", "off_topic",
                       f"未命中{miss}，判为与观察范围无关（零成本粗筛，未消耗 LLM）")
                continue

        # ---- 信源归属 ----
        # AIHOT 通道的条目：先按原文域名解析注册表；解析不到时不落到默认 D，
        # 而是采信 AIHOT 提供的 source.name 与据此判定的一手性——
        # 「OpenAI 官网动态」是一手，不该因为不在我们注册表里就降成聚合转载。
        resolved = C.attribute(it["url"], domain_idx)
        if it.get("_channel") == "aihot":
            src = resolved
            scls = (src["source_class"] if src else it.get("_aihot_class", "C"))
        else:
            src = it.get("_src") or resolved
            scls = src["source_class"] if src else cfg["verify"]["unknown_source_class"]

        # ---- 原始出处归因（技术方案 §4.0）----
        orig, eff = None, scls
        cand = C.detect_original_source(blob, patterns)
        if cand and (not src or cand != src["id"]):
            orig = cand
            oc = src_by_id.get(cand, {}).get("source_class")
            if oc and oc < eff:              # A<B<C<D<E，取更靠近一手的
                eff = oc
                stats["attributed"] += 1

        # ---- L2a 标题归一化精确匹配（抓跨站转载）----
        ntitle = C.normalize_title(title)
        if ntitle and ntitle in known_titles:
            stats["syndication"] += 1
            reject(it, "title_dedup", "syndication",
                   f"标题归一化后与 {known_titles[ntitle]} 一致（剥离站名后缀），判为转载",
                   merged=known_titles[ntitle])
            continue

        # ---- L2b SimHash 正文近重复（抓改标题的转载）----
        sh = C.simhash(blob)
        dup_of = None
        for oid, oh in known_hashes + batch_hashes:
            if oh and C.hamming(sh, oh) <= thr:
                dup_of = oid
                break
        if dup_of:
            stats["syndication"] += 1
            reject(it, "simhash", "syndication",
                   f"正文与 {dup_of} 近重复（汉明距离≤{thr}），判为转载", merged=dup_of)
            continue

        store.upsert("items", {
            "id": iid, "url": it["url"], "url_canonical": canon, "title": title,
            "content": (it.get("content") or "")[:cfg["search"]["content_max_chars"]],
            "source_id": src["id"] if src else None, "source_class": scls,
            "original_source": orig, "effective_class": eff,
            "published_at": pub, "time_source": tsrc,
            "discovered_at": now_iso(), "run_id": run_id,
            "channel": it.get("_channel"), "simhash": sh, "status": "pending",
        })
        known_urls.add(canon)
        known_titles[ntitle] = iid
        batch_hashes.append((iid, sh))
        stats["accepted"] += 1

    return stats


# ==================================================================== 报告
def print_funnel(stats: dict, cost: dict, errors: list[str], store: Store,
                 run_id: str, llm_stats: dict | None = None):
    W = 66
    print("\n" + "=" * W)
    print("漏斗回放")
    print("=" * W)
    r, a = stats["raw"], stats["accepted"]
    print(f"  采集原始条目          {r:>5}")
    print(f"    ├─ URL 重复          -{stats['duplicate_url']:>4}   （此前已采集，零成本）")
    print(f"    ├─ 超出时间窗口      -{stats['stale']:>4}")
    print(f"    ├─ 关键词粗筛淘汰    -{stats['off_topic']:>4}   （与观察范围无关，未耗 LLM）")
    print(f"    ├─ 转载近重复        -{stats['syndication']:>4}   （SimHash 判定）")
    print(f"    └─ 缺标题/链接       -{stats['no_title']:>4}")
    print(f"  入库待处理            {a:>5}   （通过率 {a / r * 100:.0f}%）" if r else "")
    if stats["attributed"]:
        print(f"  原始出处归因命中      {stats['attributed']:>5}   （转载→回溯到一手信源）")

    rows = store.q("""SELECT channel, COUNT(*) n FROM items WHERE run_id=?
                      GROUP BY channel ORDER BY n DESC""", (run_id,))
    if rows:
        print("\n  按通道：" + "  ".join(f"{r['channel']}={r['n']}" for r in rows))

    rows = store.q("""SELECT COALESCE(effective_class,'?') c, COUNT(*) n FROM items
                      WHERE run_id=? GROUP BY c ORDER BY c""", (run_id,))
    if rows:
        print("  按一手性：" + "  ".join(f"{r['c']}类={r['n']}" for r in rows))

    rows = store.q("""SELECT COALESCE(time_source,'缺失') t, COUNT(*) n FROM items
                      WHERE run_id=? GROUP BY t""", (run_id,))
    if rows:
        print("  时间口径：" + "  ".join(f"{r['t']}={r['n']}" for r in rows))

    if llm_stats:
        v = llm_stats["verify"]
        print(f"\n  事件 {v['events']} 个")
        print("  置信度：" + "  ".join(
            f"{k}={v[k]}" for k in ("官方确认·多源印证", "官方一手", "多源已验证",
                                    "深度单源", "单源待确认") if v.get(k)))
        print("  状态：  " + "  ".join(
            f"{k}={v[k]}" for k in ("新增", "延续", "静默", "旧闻/背景") if v.get(k)))
        m = llm_stats["merge"]
        if m.get("orphan_rescued"):
            print(f"  孤儿挽回：{m['orphan_rescued']} 条未被模型分组的候选独立成事件")
        if v.get("backdated"):
            print(f"  回溯性旧闻拦截：{v['backdated']} 个事件的 event_date 早于观察窗口，"
                  f"判为旧闻而非新增")
        if v.get("stage_discounted"):
            print(f"  阶段打折：{v['stage_discounted']} 个事件的官方口径被第三方下修")
        if v.get("pending_review"):
            print(f"  待人工复核：{v['pending_review']} 个单源事件")
        u = llm_stats["llm_usage"]
        print(f"\n  LLM：{u['calls']} 次调用（缓存命中 {u['cache_hits']}），"
              f"in {u['in_tokens']} / out {u['out_tokens']} tokens，约 ¥{llm_stats['llm_cost_cny']}")
    if cost["queries"]:
        print(f"  Exa 成本：${cost['total']} / {cost['queries']} 组查询")
    if errors:
        print(f"\n  采集告警 {len(errors)} 条（不阻断）：")
        for e in errors[:8]:
            print(f"    · {e}")
        if len(errors) > 8:
            print(f"    · …另有 {len(errors) - 8} 条")
    print("=" * W)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="ecommerce")
    ap.add_argument("--stage", default="all", choices=["all", "collect"])
    ap.add_argument("--mock", action="store_true",
                    help="仅用 LLM 缓存回放，零 API key 复现")
    ap.add_argument("--rebuild", action="store_true", help="从 JSONL 重建 intel.db 后退出")
    ap.add_argument("--no-exa", action="store_true", help="跳过 Exa 通道（零成本）")
    args = ap.parse_args()

    # CI 是全新 checkout，intel.db 被 gitignore。若不先从 JSONL 重建，
    # known_urls / known_titles 全空，跨期去重会失效并把历史条目重新入库一遍。
    from pipeline.store import DATA, DB_PATH
    auto_rebuild = not DB_PATH.exists() and (DATA / "items.jsonl").exists()

    store = Store()
    if auto_rebuild:
        c = store.rebuild()
        print(f"intel.db 不存在，已从 JSONL 重建："
              + "  ".join(f"{k}={v}" for k, v in c.items() if v))

    if args.rebuild:
        counts = store.rebuild()
        print("已从 JSONL 重建 intel.db：")
        for t, n in counts.items():
            if n:
                print(f"  {t:10} {n}")
        store.close()
        return 0

    cfg, srccfg = load_config(args.dataset)
    run_id = "RUN-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    started = now_iso()

    print(f"运行 {run_id}   数据集={args.dataset}   "
          f"采集窗口={cfg.get('collect_window_days', cfg['window_days'])}天 / "
          f"观察窗口={cfg['window_days']}天")

    # 信源注册表落库（配置即数据，可被 SQL 查询）
    for s in srccfg["sources"]:
        store.upsert("sources", s)
    store.commit()

    print("\n[S0] 采集")
    raw, cost, errors = stage_collect(cfg, srccfg, use_exa=not args.no_exa)
    print(f"  → 原始条目 {len(raw)}")

    print("\n[S1] 三层去重")
    stats = stage_dedup(store, raw, cfg, srccfg, run_id)
    print(f"  → 入库 {stats['accepted']}，拒绝 "
          f"{stats['raw'] - stats['accepted']}（全部留痕于 rejects 台账）")

    llm_stats = {}
    if args.stage == "all":
        llm = LLM(mock=args.mock)

        print("\n[S2] LLM 预筛（便宜档，吃最大调用量）")
        ps = PR.stage_prescreen(store, llm, cfg, run_id)
        print(f"  → 保留 {ps['keep']}，淘汰 {ps['drop']}"
              + (f"，异常 {ps['error']}" if ps["error"] else ""))

        print("\n[S3] 结构化抽取（强档，提示词按信源一手性分流）")
        ex = PR.stage_extract(store, llm, cfg, run_id, srccfg)
        print(f"  → 抽出 {ex['extracted']} 条事件草稿"
              f"（fact {ex['facts']} / inference {ex['inferences']}），"
              f"D类跳过 {ex['skipped_D']}，判无关 {ex['irrelevant']}")

        print("\n[S4] 事件归并（按公司通读聚类）")
        mg = PR.stage_merge(store, llm, cfg, run_id)
        print(f"  → {mg['candidates']} 条候选归并为 {mg['events']} 个事件"
              f"（合并掉 {mg['merged_away']} 条重复报道），关系边 {mg['relations']}")

        print("\n[S5] 核验（纯规则）")
        vf = PR.stage_verify(store, cfg, srccfg)
        print(f"  → {vf['events']} 个事件完成置信度/阶段/状态判定")

        print("\n[S6] 关系边补全（规则）")
        eg = IN.stage_edges(store)
        print(f"  → 新增 same_actor_track 边 {eg['rule_edges']}")

        print("\n[S7] 洞察合成（周报）")
        ins = IN.stage_insight(store, llm, cfg, run_id)
        if ins.get("error"):
            print(f"  → {ins['error']}")
        else:
            print(f"  → 窗口 {ins['period']}，输入 {ins['events_in']} 事件")
            print(f"     关键发现 {ins['key_findings']} · 借鉴建议 {ins['implications']}"
                  f" · 观察清单 {ins['watchlist']} · 延续性检查 {ins.get('continuity', 0)}")
            print(f"     主线：{ins.get('headline', '')}")

        llm_stats = {"prescreen": ps, "extract": ex, "merge": mg, "verify": vf,
                     "edges": eg, "insight": ins,
                     "llm_usage": llm.usage, "llm_cost_cny": llm.cost_estimate()}

    store.upsert("runs", {
        "id": run_id, "started_at": started, "finished_at": now_iso(),
        "mode": "live", "dataset": args.dataset,
        "stats": {**stats, **llm_stats, "collect_errors": errors},
        "cost": cost, "config_hash": config_hash(cfg, srccfg),
    })
    store.commit()

    print_funnel(stats, cost, errors, store, run_id, llm_stats)

    counts = store.dump()
    print(f"\n已同步 JSONL 真相源：" +
          "  ".join(f"{t}={n}" for t, n in counts.items() if n))
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
