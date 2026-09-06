#!/usr/bin/env python3
"""信源可采性探测 · 第二轮 (W1 D1-D2)

第一轮是首页级探测，有三个盲区，本轮逐一验证：

  1. 微信公众号 —— 测 RSSHub 各微信路由族是否还活着（第一轮指向 mp.weixin.qq.com
     首页必然被反爬拦，结论无效）
  2. RSSHub 路由补测 —— 第一轮退到 Exa 的信源，补测它们的 RSSHub 路由
  3. 文章级验证 —— 付费墙与 SSR 要在文章页判定，首页判不出来

用法: python3.13 scripts/probe_round2.py
输出: config/source_probe_round2.json
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "config" / "source_probe_round2.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# 第一轮实测存活的实例
INSTANCES = ["https://rsshub.rssforever.com", "https://hub.slarker.me"]

# ---- 1. 微信公众号路由族（RSSHub 的微信路由历年大量失效，需实测哪族还活着）----
WECHAT_ROUTES = [
    ("/wechat/ce/pWlOoxJKzFOd6H9GkQ8Uxw", "wechat/ce · 微信公众号（第三方镜像）"),
    ("/wechat/ershicimi/lianggezhong", "wechat/ershicimi · 二十次幂"),
    ("/wechat/wemp/1", "wechat/wemp · Wemp"),
    ("/wechat/feeddd/1", "wechat/feeddd · feeddd 项目"),
    ("/wechat/uread/1", "wechat/uread · 优读"),
    ("/wechat/miniprogram/1", "wechat/miniprogram"),
]

# ---- 2. 第一轮退到 Exa 的信源，补测 RSSHub 路由 ----
FALLBACK_ROUTES = [
    ("ebrun",      "亿邦动力",   ["/ebrun/news", "/ebrun/index", "/ebrun"]),
    ("jiqizhixin", "机器之心",   ["/jiqizhixin/all", "/jiqizhixin/category/ai", "/jiqizhixin"]),
    ("yicai",      "第一财经",   ["/yicai/brief", "/yicai/headline", "/yicai/news"]),
    ("lanjinger",  "蓝鲸财经",   ["/lanjinger/index", "/lanjinger/news", "/lanjinger"]),
    ("100ec",      "网经社",     ["/100ec/index", "/100ec/news", "/100ec"]),
    ("xiaohongshu","小红书",     ["/xiaohongshu/user/notes", "/xiaohongshu/board"]),
    ("caixin",     "财新",       ["/caixin/latest", "/caixin/index", "/caixin"]),
    ("latepost",   "晚点",       ["/latepost/index", "/latepost/recommend", "/latepost"]),
    ("36kr",       "36氪·资讯",  ["/36kr/news/ai", "/36kr/motif/452686275073", "/36kr/hot-list"]),
]

# ---- 3. 文章级验证（付费墙与 SSR 只能在文章页判定）----
ARTICLE_TESTS = [
    ("caixin",   "财新",       "https://www.caixin.com/2026-09-01/"),
    ("latepost", "晚点",       "https://www.latepost.com/news/dj_detail?id=1"),
    ("ebrun",    "亿邦动力",   "https://www.ebrun.com/20260101/1.shtml"),
    ("yicai",    "第一财经",   "https://www.yicai.com/news/"),
]

PAYWALL_HINTS = ["登录后阅读", "登录后查看", "请先登录", "付费阅读", "订阅后阅读",
                 "开通会员", "成为会员", "购买后阅读", "该内容需要付费", "财新通"]


def fetch(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            return r.status, r.read(400_000)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(20_000)
        except Exception:                                     # noqa: BLE001
            return e.code, b""
    except Exception as e:                                    # noqa: BLE001
        return type(e).__name__, b""


def decode(b: bytes) -> str:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="ignore")


def feed_items(body: bytes) -> int:
    head = body[:3000].lstrip()
    if not (head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head):
        return 0
    t = decode(body)
    return len(re.findall(r"(?i)<item[\s>]", t)) or len(re.findall(r"(?i)<entry[\s>]", t))


def feed_titles(body: bytes, n: int = 3) -> list[str]:
    t = decode(body)
    return [re.sub(r"<!\[CDATA\[|\]\]>", "", m).strip()
            for m in re.findall(r"(?is)<title>(.*?)</title>", t)[1:n + 1]]


def cn_len(html: str) -> int:
    s = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return len(re.findall(r"[一-鿿]", s))


# ---------------------------------------------------------------- 1. 微信
def test_wechat() -> list[dict]:
    print("\n[1] 微信公众号路由族探测")
    print("-" * 92)
    out = []
    for route, label in WECHAT_ROUTES:
        best = None
        for inst in INSTANCES:
            st, body = fetch(inst + route, timeout=20)
            n = feed_items(body) if isinstance(st, int) and st == 200 else 0
            snippet = decode(body[:400]).replace("\n", " ")[:110] if n == 0 else ""
            rec = {"route": route, "label": label, "instance": inst,
                   "status": str(st), "items": n, "snippet": snippet}
            if best is None or n > best["items"]:
                best = rec
            if n > 0:
                break
        out.append(best)
        mark = "OK  " if best["items"] > 0 else "FAIL"
        print(f"  [{mark}] {label:42} status={best['status']:<14} items={best['items']}")
        if best["items"] == 0 and best["snippet"]:
            print(f"         └─ {best['snippet']}")
    return out


# ---------------------------------------------------------------- 2. 补测路由
def test_fallback_routes() -> list[dict]:
    print("\n[2] 第一轮退 Exa 信源的 RSSHub 路由补测")
    print("-" * 92)
    results = []

    def probe(sid, name, routes):
        for route in routes:
            for inst in INSTANCES:
                st, body = fetch(inst + route, timeout=20)
                if isinstance(st, int) and st == 200:
                    n = feed_items(body)
                    if n > 0:
                        return {"id": sid, "name": name, "route": route,
                                "instance": inst, "items": n,
                                "titles": feed_titles(body)}
        return {"id": sid, "name": name, "route": None, "items": 0, "titles": []}

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(probe, s, n, r) for s, n, r in FALLBACK_ROUTES]
        for f in as_completed(futs):
            results.append(f.result())

    order = {s: i for i, (s, _, _) in enumerate(FALLBACK_ROUTES)}
    results.sort(key=lambda r: order[r["id"]])
    for r in results:
        if r["items"]:
            print(f"  [OK  ] {r['name']:14} {r['route']:34} {r['items']} 条")
            for t in r["titles"][:2]:
                print(f"           · {t[:64]}")
        else:
            print(f"  [FAIL] {r['name']:14} 所有候选路由均无效")
    return results


# ---------------------------------------------------------------- 3. 文章级
def test_articles() -> list[dict]:
    print("\n[3] 文章级验证（付费墙 / SSR 真实性）")
    print("-" * 92)
    out = []
    for sid, name, url in ARTICLE_TESTS:
        st, body = fetch(url, timeout=15)
        html = decode(body) if body else ""
        n = cn_len(html)
        pay = [h for h in PAYWALL_HINTS if h in html]
        rec = {"id": sid, "name": name, "url": url, "status": str(st),
               "cn_chars": n, "paywall_hits": pay}
        out.append(rec)
        flag = f"付费墙({','.join(pay[:2])})" if pay else ("正文可读" if n > 500 else "内容稀薄")
        print(f"  {name:12} status={str(st):<14} 中文字数={n:<7} {flag}")
    return out


def main() -> int:
    t0 = time.time()
    print("=" * 92)
    print("信源可采性探测 · 第二轮")
    print("=" * 92)

    wechat = test_wechat()
    fallback = test_fallback_routes()
    articles = test_articles()

    wechat_ok = [w for w in wechat if w["items"] > 0]
    recovered = [r for r in fallback if r["items"] > 0]

    print("\n" + "=" * 92)
    print("小结")
    print("=" * 92)
    print(f"  微信公众号可用路由族 : {len(wechat_ok)} / {len(wechat)}")
    if wechat_ok:
        for w in wechat_ok:
            print(f"      → {w['label']}  ({w['instance']})")
    else:
        print("      → 全部失效。公众号无法直采，需改走替代方案")
    print(f"  补测挽回的信源       : {len(recovered)} / {len(fallback)}")
    for r in recovered:
        print(f"      → {r['name']}  {r['route']}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "wechat_routes": wechat,
        "fallback_routes": fallback,
        "article_tests": articles,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {OUT.relative_to(ROOT)}  (耗时 {time.time() - t0:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
