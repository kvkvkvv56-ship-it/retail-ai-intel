#!/usr/bin/env python3
"""信源可采性探测 (W1 D1-D2)

对每个候选信源实测三条采集路径，产出带证据的信源注册表初稿：

  1. 原生 RSS  —— HTML autodiscovery (<link rel=alternate>) + 常见路径探测
  2. 直接抓取  —— HTTP 状态 / 是否服务端渲染 / 付费墙特征
  3. RSSHub    —— 多公共实例可用性测试

用法:
    python3.13 scripts/probe_sources.py            # 全量探测
    python3.13 scripts/probe_sources.py --quick    # 只测 RSS 与直抓，跳过 RSSHub

输出:
    config/source_probe.json   机器可读结果
    控制台                      人读报告
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "config" / "source_probe.json"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT = 12

# 未验证证书不阻断探测——我们只关心可达性与内容形态
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# 常见 RSS 路径（按命中率排序）
RSS_PATHS = [
    "/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml",
    "/index.xml", "/feed/", "/rss/", "/api/rss",
]

# 付费墙 / 登录墙特征词
PAYWALL_HINTS = [
    "登录后阅读", "登录后查看", "请先登录", "付费阅读", "订阅后阅读",
    "开通会员", "成为会员", "购买后阅读", "subscribe to read",
    "sign in to continue", "该内容需要付费",
]

# 反爬 / 验证特征词
BLOCKED_HINTS = [
    "访问验证", "安全验证", "滑动验证", "captcha", "请开启JavaScript",
    "environment is abnormal", "当前环境异常",
]

# 待测 RSSHub 公共实例
RSSHUB_INSTANCES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rss.shab.fun",
    "https://hub.slarker.me",
    "https://rsshub.pseudoyu.com",
]


# --------------------------------------------------------------------------
# 候选信源清单
#   class: A 当事方直述 / B 原创深度 / C 常规报道 / D 聚合转载 / E 观点评论
#   rsshub: RSSHub 候选路由（None = 该源不走 RSSHub）
# --------------------------------------------------------------------------
@dataclass
class Candidate:
    id: str
    name: str
    cls: str
    home: str
    org: str
    rsshub: str | None = None
    affiliated_with: str | None = None
    note: str = ""


CANDIDATES: list[Candidate] = [
    # ---------- B · 原创深度报道（情报密度最高，优先级最高）----------
    Candidate("latepost", "晚点 LatePost", "B", "https://www.latepost.com",
              "media_latepost", "/latepost/index", note="字节/阿里独家最多，电商赛道最强"),
    Candidate("caixin", "财新网", "B", "https://www.caixin.com",
              "media_caixin", None, note="监管政策深度，预期硬付费墙"),
    Candidate("36kr", "36氪", "B", "https://36kr.com",
              "media_36kr", "/36kr/newsflashes", note="含智能涌现 AI 垂类"),
    Candidate("yicai", "第一财经", "B", "https://www.yicai.com",
              "media_yicai", None, note="财经调查"),
    Candidate("tmtpost", "钛媒体", "B", "https://www.tmtpost.com",
              "media_tmtpost", "/tmtpost/homepage", note="科技商业深度"),
    Candidate("wallstreetcn", "华尔街见闻", "B", "https://wallstreetcn.com",
              "media_wallstreetcn", "/wallstreetcn/news/global", note="财报电话会覆盖好"),

    # ---------- C · 常规媒体报道（覆盖广度）----------
    Candidate("ebrun", "亿邦动力", "C", "https://www.ebrun.com",
              "media_ebrun", None, note="电商垂类最专业"),
    Candidate("100ec", "网经社", "C", "https://www.100ec.cn",
              "media_100ec", None, note="电商垂直媒体"),
    Candidate("huxiu", "虎嗅", "C", "https://www.huxiu.com",
              "media_huxiu", "/huxiu/article", note="科技商业"),
    Candidate("jiemian", "界面新闻", "C", "https://www.jiemian.com",
              "media_jiemian", "/jiemian/list/78", note="财经媒体"),
    Candidate("qbitai", "量子位", "C", "https://www.qbitai.com",
              "media_qbitai", "/qbitai/category/资讯", note="AI 垂类"),
    Candidate("jiqizhixin", "机器之心", "C", "https://www.jiqizhixin.com",
              "media_jiqizhixin", "/jiqizhixin/all", note="AI 垂类"),
    Candidate("pingwest", "品玩", "C", "https://www.pingwest.com",
              "media_pingwest", "/pingwest/status", note="科技媒体"),
    Candidate("leiphone", "雷峰网", "C", "https://www.leiphone.com",
              "media_leiphone", "/leiphone/category/ai", note="含零售数智化垂类"),
    Candidate("lanjinger", "蓝鲸财经", "C", "https://www.lanjinger.com",
              "media_lanjinger", None, note="财经快讯"),
    Candidate("dsb", "电商报", "C", "https://www.dsb.cn",
              "media_dsb", None, note="电商资讯"),
    Candidate("sina_tech", "新浪科技", "C", "https://tech.sina.com.cn",
              "media_sina", "/sina/rollnews", note="科技快讯"),

    # ---------- E · 观点评论（只抽判断，不抽事实）----------
    Candidate("kazik", "数字生命卡兹克", "E", "https://mp.weixin.qq.com",
              "kol_kazik", None, note="公众号，需 RSSHub 微信路由或镜像"),
    Candidate("42章经", "42章经", "E", "https://mp.weixin.qq.com",
              "kol_42zhangjing", None, note="公众号"),

    # ---------- A · 当事方直述（锚点）----------
    Candidate("alibaba_ir", "阿里巴巴集团 IR", "A", "https://www.alibabagroup.com",
              "ali_official", None, "taobao", note="财报与战略公告"),
    Candidate("kuaishou_ir", "快手 IR", "A", "https://ir.kuaishou.com",
              "kuaishou_official", None, "kuaishou", note="港股财报"),
    Candidate("oceanengine", "巨量引擎", "A", "https://www.oceanengine.com",
              "oceanengine_official", None, "douyin", note="字节商业产品"),
    Candidate("xiaohongshu", "小红书", "A", "https://www.xiaohongshu.com",
              "xhs_official", None, "xiaohongshu", note="治理与规则公告"),
    Candidate("alibaba_talent", "阿里招聘", "A", "https://talent.alibaba.com",
              "ali_official", None, "taobao", note="组织信号：AI 岗位结构"),
    Candidate("bytedance_jobs", "字节招聘", "A", "https://jobs.bytedance.com",
              "bytedance_official", None, "douyin", note="组织信号"),
]


# --------------------------------------------------------------------------
# 探测结果
# --------------------------------------------------------------------------
@dataclass
class ProbeResult:
    id: str
    name: str
    cls: str
    org: str
    home: str
    affiliated_with: str | None
    note: str

    http_status: int | str = "-"
    ssr: bool = False              # 是否服务端渲染出正文
    text_len: int = 0
    paywall: bool = False
    blocked: bool = False

    native_rss: list[str] = field(default_factory=list)
    rsshub_route: str | None = None
    rsshub_ok: bool = False
    rsshub_items: int = 0

    channel: str = "exa"           # 最终建议通道
    verdict: str = ""              # 人读结论
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# HTTP 工具
# --------------------------------------------------------------------------
def fetch(url: str, timeout: int = TIMEOUT) -> tuple[int | str, bytes, str]:
    """返回 (status, body, final_url)。失败时 status 为错误字符串。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            return r.status, r.read(400_000), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, b"", url
    except Exception as e:                                    # noqa: BLE001
        return f"{type(e).__name__}", b"", url


def decode(body: bytes) -> str:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="ignore")


def visible_text_len(html: str) -> int:
    """粗估服务端渲染出的可见中文正文长度。"""
    s = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return len(re.findall(r"[一-鿿]", s))


def discover_rss(home: str, html: str) -> list[str]:
    """从 HTML autodiscovery 标签中提取 RSS 地址。"""
    found = []
    for m in re.finditer(r"(?is)<link[^>]+>", html):
        tag = m.group(0)
        if "alternate" not in tag.lower():
            continue
        if not re.search(r"(?i)type\s*=\s*[\"'][^\"']*(rss|atom)\+xml", tag):
            continue
        href = re.search(r"(?i)href\s*=\s*[\"']([^\"']+)[\"']", tag)
        if href:
            found.append(urllib.parse.urljoin(home, href.group(1)))
    return found


def looks_like_feed(body: bytes) -> int:
    """判断响应是否是 feed，返回条目数（0 表示不是）。"""
    head = body[:3000].lstrip()
    if not (head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head):
        return 0
    text = decode(body)
    n = len(re.findall(r"(?i)<item[\s>]", text)) or len(re.findall(r"(?i)<entry[\s>]", text))
    return n


# --------------------------------------------------------------------------
# 单信源探测
# --------------------------------------------------------------------------
def probe_native_rss(home: str, html: str) -> list[str]:
    """autodiscovery 优先，其次穷举常见路径。"""
    hits = []
    for url in discover_rss(home, html):
        st, body, _ = fetch(url, timeout=8)
        if st == 200 and looks_like_feed(body):
            hits.append(url)
    if hits:
        return hits
    base = home.rstrip("/")
    for path in RSS_PATHS:
        st, body, _ = fetch(base + path, timeout=6)
        if st == 200 and looks_like_feed(body):
            hits.append(base + path)
            break                       # 命中一个就够
    return hits


def probe_one(c: Candidate) -> ProbeResult:
    r = ProbeResult(id=c.id, name=c.name, cls=c.cls, org=c.org, home=c.home,
                    affiliated_with=c.affiliated_with, note=c.note,
                    rsshub_route=c.rsshub)

    status, body, _ = fetch(c.home)
    r.http_status = status
    if not isinstance(status, int) or status != 200:
        r.errors.append(f"首页不可达: {status}")
    else:
        html = decode(body)
        r.text_len = visible_text_len(html)
        r.ssr = r.text_len > 800
        low = html.lower()
        r.paywall = any(h.lower() in low for h in PAYWALL_HINTS)
        r.blocked = any(h.lower() in low for h in BLOCKED_HINTS)
        try:
            r.native_rss = probe_native_rss(c.home, html)
        except Exception as e:                                # noqa: BLE001
            r.errors.append(f"RSS 探测异常: {type(e).__name__}")
    return r


# --------------------------------------------------------------------------
# RSSHub 实例可用性
# --------------------------------------------------------------------------
def probe_rsshub_instances() -> list[tuple[str, str, int]]:
    """返回 [(实例, 状态, 探测路由条目数)]"""
    out = []
    for inst in RSSHUB_INSTANCES:
        st, body, _ = fetch(f"{inst}/36kr/newsflashes", timeout=15)
        n = looks_like_feed(body) if isinstance(st, int) and st == 200 else 0
        out.append((inst, str(st), n))
    return out


def probe_rsshub_routes(results: list[ProbeResult], instance: str) -> None:
    """在可用实例上逐条测试候选路由。"""
    def one(r: ProbeResult):
        if not r.rsshub_route:
            return
        url = instance.rstrip("/") + r.rsshub_route
        st, body, _ = fetch(url, timeout=20)
        if isinstance(st, int) and st == 200:
            n = looks_like_feed(body)
            r.rsshub_ok, r.rsshub_items = n > 0, n
        else:
            r.errors.append(f"RSSHub {r.rsshub_route}: {st}")

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(as_completed([ex.submit(one, r) for r in results]))


# --------------------------------------------------------------------------
# 结论判定
# --------------------------------------------------------------------------
def decide(r: ProbeResult) -> None:
    if r.native_rss:
        r.channel, r.verdict = "rss", f"原生 RSS 可用 ({len(r.native_rss)} 个)"
    elif r.rsshub_ok:
        r.channel, r.verdict = "rsshub", f"RSSHub 可用 ({r.rsshub_items} 条)"
    elif r.blocked:
        r.channel, r.verdict = "exa", "有反爬验证，只能靠 Exa 间接覆盖"
    elif r.paywall:
        r.channel, r.verdict = "exa", "付费墙，仅标题+导语，标记 partial"
    elif r.ssr:
        r.channel, r.verdict = "direct", f"服务端渲染可直抓 ({r.text_len} 字)"
    elif isinstance(r.http_status, int) and r.http_status == 200:
        r.channel, r.verdict = "exa", "前端渲染，直抓拿不到正文，走 Exa"
    else:
        r.channel, r.verdict = "exa", f"不可达 ({r.http_status})，仅 Exa"


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def report(results: list[ProbeResult], instances: list[tuple[str, str, int]]) -> None:
    print("\n" + "=" * 100)
    print("RSSHub 公共实例可用性")
    print("=" * 100)
    for inst, st, n in instances:
        mark = "OK " if n > 0 else "FAIL"
        print(f"  [{mark}] {inst:38} status={st:<24} items={n}")

    print("\n" + "=" * 100)
    print("信源可采性")
    print("=" * 100)
    print(f"{'类':<3}{'信源':<16}{'HTTP':<8}{'SSR':<6}{'RSS':<5}{'Hub':<5}{'通道':<9}结论")
    print("-" * 100)
    for cls in ("A", "B", "C", "E"):
        rows = [r for r in results if r.cls == cls]
        if not rows:
            continue
        for r in rows:
            print(f"{r.cls:<3}{r.name:<16}{str(r.http_status):<8}"
                  f"{('是' if r.ssr else '否'):<6}"
                  f"{(str(len(r.native_rss)) if r.native_rss else '-'):<5}"
                  f"{('OK' if r.rsshub_ok else '-'):<5}"
                  f"{r.channel:<9}{r.verdict}")
        print("-" * 100)

    by_ch: dict[str, int] = {}
    for r in results:
        by_ch[r.channel] = by_ch.get(r.channel, 0) + 1
    print("\n通道分布: " + " | ".join(f"{k}={v}" for k, v in sorted(by_ch.items())))
    print(f"总计 {len(results)} 个信源\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过 RSSHub 探测")
    args = ap.parse_args()

    t0 = time.time()
    print(f"探测 {len(CANDIDATES)} 个候选信源…")

    results: list[ProbeResult] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(probe_one, c): c for c in CANDIDATES}
        for f in as_completed(futs):
            c = futs[f]
            try:
                results.append(f.result())
            except Exception as e:                            # noqa: BLE001
                print(f"  !! {c.name} 探测失败: {type(e).__name__}: {e}")
    results.sort(key=lambda r: (r.cls, r.id))

    instances: list[tuple[str, str, int]] = []
    if not args.quick:
        print("测试 RSSHub 公共实例…")
        instances = probe_rsshub_instances()
        alive = next((i for i, _, n in instances if n > 0), None)
        if alive:
            print(f"使用实例 {alive} 测试路由…")
            probe_rsshub_routes(results, alive)
        else:
            print("!! 无可用 RSSHub 公共实例——需自建（见方案 §17）")

    for r in results:
        decide(r)

    report(results, instances)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "rsshub_instances": [
            {"url": u, "status": s, "items": n} for u, s, n in instances
        ],
        "sources": [asdict(r) for r in results],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {OUT.relative_to(ROOT)}  (耗时 {time.time() - t0:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
