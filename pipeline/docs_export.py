"""S8b 导出：docs/*.md → 前端文档站快照

文档和数据走同一条链路：落进 web/public/api/v1/docs/，由 Worker 的
snapshot() 读取。这样文档页在线上也能随仓库更新，不必依赖重新部署。

只做「读文件 + 抽标题 + 拼索引」，不在 Python 侧渲染 Markdown——
渲染放在前端，避免 Python 与 JS 两套渲染规则长期漂移。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pipeline.store import ROOT, Store

DOCS = ROOT / "docs"
OUT = ROOT / "web" / "public" / "api" / "v1" / "docs"

# 顺序即侧边栏顺序。分组用于侧边栏分节。
REGISTRY = [
    # (文件名, id, 分组, 一句话说明)
    ("使用说明.md", "guide", "交付文档",
     "五分钟看懂每个页面在回答什么问题"),
    ("作品文档.md", "overview", "交付文档",
     "场景 · 解决思路 · 效果验证，含真实运行数据与局限"),
    ("方法论.md", "methodology", "交付文档",
     "为什么这么做：分层依据、规则裁决、被实测推翻的判断"),
    ("运行说明.md", "operations", "交付文档",
     "怎么跑起来：环境变量、命令、定时任务、部署、成本"),
    ("技术方案.md", "architecture", "技术参考",
     "架构、数据模型、核验规则、API 契约"),
]

_H = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_LIVE = re.compile(r"(<!-- live:(\w+) -->\n)(.*?)(<!-- /live -->)", re.S)


# ==================================================================== 活数字
#
# 文档里的统计数字每跑一轮就过期。手工改跟不上，写死又会越来越假。
# 所以把这些数字圈进 <!-- live:xxx --> ... <!-- /live --> 区块，
# 每次导出时按当前库重算并**写回 .md 文件本身**——
# 这样 GitHub 上看到的原文和站上看到的渲染结果始终一致。

def _tbl(rows: list[tuple[str, object]]) -> str:
    return "| | |\n|---|---|\n" + "".join(f"| {k} | {v} |\n" for k, v in rows)


def _dist(store, sql: str, label: str, order: list[str] | None = None) -> str:
    d = {r[0]: r[1] for r in store.db.execute(sql)}
    keys = [k for k in (order or []) if k in d] or sorted(d, key=lambda k: -d[k])
    keys += [k for k in sorted(d, key=lambda k: -d[k]) if k not in keys]
    return (f"| {label} | 事件数 |\n|---|---|\n"
            + "".join(f"| {k or '未判定'} | {d[k]} |\n" for k in keys))


def live_blocks(store, cfg: dict | None = None) -> dict[str, str]:
    n = lambda t: store.one(f"SELECT COUNT(*) FROM {t}") or 0          # noqa: E731
    kind = {r[0]: r[1] for r in store.db.execute(
        "SELECT kind, COUNT(*) FROM claims GROUP BY 1")}
    cname = {c["id"]: c["name"] for c in (cfg or {}).get("companies", [])}
    comp = {cname.get(r[0], r[0]): r[1] for r in store.db.execute(
        "SELECT company, COUNT(*) FROM events GROUP BY 1 ORDER BY 2 DESC")}
    rev = {r[0]: r[1] for r in store.db.execute(
        "SELECT review_state, COUNT(*) FROM events GROUP BY 1")}
    orgs = {r[0]: r[1] for r in store.db.execute(
        "SELECT independent_orgs, COUNT(*) FROM events GROUP BY 1")}
    ev = n("events")
    single = orgs.get(1, 0)
    date_hi = store.one("SELECT MAX(event_date) FROM events") or "—"
    date_lo = store.one("SELECT MIN(event_date) FROM events") or "—"
    with_out = store.one("SELECT COUNT(DISTINCT source_id) FROM items") or 0

    return {
        "counts": _tbl([
            ("事件", ev),
            ("原始条目", n("items")),
            (f"断言（事实 {kind.get('fact', 0)} / 推断 {kind.get('inference', 0)}"
             f" / 建议 {kind.get('recommendation', 0)}）", n("claims")),
            ("事件关系边", n("edges")),
            ("拒绝台账", f"{n('rejects'):,}"),
            (f"登记信源（其中 {with_out} 个有实际产出）", n("sources")),
            ("周报", f"{n('reports')} 期"),
            ("复核记录", n("reviews")),
            ("运行轮次", n("runs")),
        ]) + f"\n事件日期跨度 {date_lo} ~ {date_hi}。",

        "companies": "**观察主体分布**：" + " · ".join(
            f"{k} {v}" for k, v in list(comp.items())[:8]),

        "confidence": _dist(
            store, "SELECT confidence, COUNT(*) FROM events GROUP BY 1", "置信度",
            ["官方确认·多源印证", "官方一手", "多源已验证", "深度单源", "单源待确认"])
        + f"\n独立组织数为 1 的有 {single} 个，占 "
          f"{round(single / ev * 100) if ev else 0}%。",

        "stage": _dist(store, "SELECT stage, COUNT(*) FROM events GROUP BY 1", "落地阶段",
                       ["概念宣传", "试点探索", "已上线", "规模化应用"]),

        "rejects": "| 拦截层 | 条数 |\n|---|---|\n" + "".join(
            f"| `{r[0]}` | {r[1]:,} |\n" for r in store.db.execute(
                "SELECT stage, COUNT(*) FROM rejects GROUP BY 1 ORDER BY 2 DESC")),

        "review": (f"复核态：持续观察 {rev.get('watching', 0)} · "
                   f"已确认 {rev.get('confirmed', 0)} · "
                   f"无需复核 {rev.get('none', 0)} · "
                   f"**待复核 {rev.get('pending', 0) + rev.get('suspect_duplicate', 0)}**"),

        "channels": "**采集通道分布**：" + " · ".join(
            f"{r[0]} {r[1]}" for r in store.db.execute(
                "SELECT channel, COUNT(*) FROM items GROUP BY 1 ORDER BY 2 DESC")),

        # 观察范围也走活区块：加一个主体就要改四份文档，手工同步必漏
        # （实测漏过：README 没加、运行说明计数还写着 8、交付说明写重了两遍）
        "scope": (
            "**观察主体（" + str(len((cfg or {}).get("companies", []))) + "）**："
            + " · ".join(c["name"] for c in (cfg or {}).get("companies", []))
            + "\n\n**观察领域（" + str(len((cfg or {}).get("domains", []))) + "）**："
            + " · ".join(d["name"] for d in (cfg or {}).get("domains", []))),

        "classes": "**原始条目一手性分布**：" + " · ".join(
            f"{r[0]} {r[1]}" for r in store.db.execute(
                "SELECT effective_class, COUNT(*) FROM items GROUP BY 1 ORDER BY 1")),
    }


def refresh_live(store, cfg=None) -> int:
    """把 live 区块按当前库重写回 .md 文件。返回改动的区块数。"""
    blocks = live_blocks(store, cfg)
    changed = 0
    # 交付说明在仓库根目录、不进文档站，但同样含活区块，必须一起刷
    targets = list(DOCS.glob("*.md")) + [ROOT / "README.md", ROOT / "交付说明.md"]
    for f in targets:
        if not f.exists():
            continue
        src = f.read_text(encoding="utf-8")

        def sub(m):
            nonlocal changed
            name, old = m.group(2), m.group(3)
            new = blocks.get(name)
            if new is None:
                return m.group(0)
            new = new.rstrip("\n") + "\n"
            if new != old:
                changed += 1
            return m.group(1) + new + m.group(4)

        out = _LIVE.sub(sub, src)
        if out != src:
            f.write_text(out, encoding="utf-8")
    return changed
_FM = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
_FENCE = re.compile(r"^\s*```")


def strip_frontmatter(md: str) -> tuple[str, dict]:
    """剥离 YAML frontmatter。

    不剥的话它会被 Markdown 当普通文本渲染成一段散乱的 tags/created/status，
    出现在正文最上面（线上实测就是这样）。这里只做扁平 key: value 的解析，
    docs/ 里的 frontmatter 都是这个形状，不引入 YAML 依赖。
    """
    m = _FM.match(md)
    if not m:
        return md, {}
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "-", "\t")):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return md[m.end():], meta


def outline(md: str) -> list[dict]:
    """抽 1–3 级标题做目录。

    必须跳过代码块内的 `#`——技术方案里有大量 Python 注释行以 # 开头，
    不跳过的话目录会被注释淹没。
    """
    items, in_fence = [], False
    for line in md.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _H.match(line)
        if m:
            items.append({"level": len(m.group(1)), "text": m.group(2)})
    return items


def export_docs(store=None, cfg=None) -> dict:
    # 先把文档里的活数字按当前库刷一遍，再导出
    live = refresh_live(store or Store(), cfg)

    if OUT.exists():
        for f in OUT.glob("*.json"):
            f.unlink()
    OUT.mkdir(parents=True, exist_ok=True)

    index, total, missing = [], 0, []
    for fname, did, group, desc in REGISTRY:
        src = DOCS / fname
        if not src.exists():
            missing.append(fname)
            continue
        md, fm = strip_frontmatter(src.read_text(encoding="utf-8"))
        ol = outline(md)
        # 首个一级标题作为文档标题，没有就退回文件名
        title = next((i["text"] for i in ol if i["level"] == 1), src.stem)
        body = {"id": did, "title": title, "group": group, "description": desc,
                "source": f"docs/{fname}", "outline": ol, "markdown": md,
                "updated": fm.get("created"), "status": fm.get("status")}
        txt = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        (OUT / f"{did}.json").write_text(txt, encoding="utf-8")
        total += len(txt)

        index.append({"id": did, "title": title, "group": group,
                      "description": desc, "source": f"docs/{fname}",
                      "lines": md.count("\n") + 1,
                      # 索引里只带二级标题，够侧边栏展开用，不必背全量目录
                      "sections": [i["text"] for i in ol if i["level"] == 2]})

    idx = {"total": len(index),
           "groups": list(dict.fromkeys(d["group"] for d in index)),
           "results": index}
    txt = json.dumps(idx, ensure_ascii=False, separators=(",", ":"))
    (OUT.parent / "docs.json").write_text(txt, encoding="utf-8")
    return {"docs": len(index), "missing": missing, "live": live,
            "kb": round((total + len(txt)) / 1024)}


if __name__ == "__main__":
    # 单独跑时也要带上 config，否则 scope 这类依赖配置的活区块会被刷成空
    import sys
    sys.path.insert(0, str(ROOT))
    from pipeline.run import load_config
    _cfg, _ = load_config("ecommerce")
    r = export_docs(Store(), _cfg)
    print(f"已导出文档快照 {r['docs']} 份 → web/public/api/v1/docs/  ({r['kb']} KB)")
    if r["live"]:
        print(f"  活数字区块已刷新 {r['live']} 处")
    if r["missing"]:
        print(f"  ⚠️ 缺失：{'、'.join(r['missing'])}")
