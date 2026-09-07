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

from pipeline.store import ROOT

DOCS = ROOT / "docs"
OUT = ROOT / "web" / "public" / "api" / "v1" / "docs"

# 顺序即侧边栏顺序。分组用于侧边栏分节。
REGISTRY = [
    # (文件名, id, 分组, 一句话说明)
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


def export_docs() -> dict:
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
    return {"docs": len(index), "missing": missing,
            "kb": round((total + len(txt)) / 1024)}


if __name__ == "__main__":
    r = export_docs()
    print(f"已导出文档快照 {r['docs']} 份 → web/public/api/v1/docs/  ({r['kb']} KB)")
    if r["missing"]:
        print(f"  ⚠️ 缺失：{'、'.join(r['missing'])}")
