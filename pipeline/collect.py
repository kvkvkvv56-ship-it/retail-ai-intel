"""S0 采集：四通道

  rss     原生 / 直连 RSS（含 wechat2rss 公众号镜像）
  rsshub  经 RSSHub 实例，双实例 fallback
  direct  服务端渲染页面直抓
  exa     Exa 搜索广度召回

设计见 docs/技术方案.md §4，通道分配依据 docs/信源实测报告.md。

三条硬规则：
  1. 正文只保留截断摘录（content_max_chars），不存全文（版权，见 README 边界声明）
  2. 时间三级口径：exact（结构化元数据）> parsed（页面解析）> inferred（LLM 推断）
     inferred 不参与窗口硬判定
  3. 单个信源失败不阻断整轮，记入 run.stats.collect_errors
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# URL 规范化时剥离的跟踪参数
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "from", "spm", "share_token", "share_from", "scene", "srcid", "chksm",
    "sharer_shareinfo", "sharer_sharetime", "ref", "referrer", "_hsenc",
}


# ============================================================ HTTP
def fetch(url: str, timeout: int = 20, headers: dict | None = None):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            return r.status, r.read(600_000)
    except urllib.error.HTTPError as e:
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


# ============================================================ 规范化与指纹
def canonical_url(url: str) -> str:
    """去跟踪参数、去锚点、统一小写域名。用于 L1 精确去重。"""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    qs = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=False)
          if k.lower() not in TRACKING_PARAMS]
    return urllib.parse.urlunsplit((
        p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/",
        urllib.parse.urlencode(sorted(qs)), "",
    ))


def item_id(canon: str) -> str:
    return "it_" + hashlib.sha1(canon.encode()).hexdigest()[:14]


def simhash(text: str, bits: int = 64) -> str:
    """正文 SimHash，用于 L2 转载检测（汉明距离 ≤ 3 判为近重复）。"""
    toks = re.findall(r"[一-鿿]{2}|[a-zA-Z]{3,}", text)
    if not toks:
        return "0" * 16
    v = [0] * bits
    for t in toks:
        h = int(hashlib.md5(t.encode()).hexdigest(), 16)
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    return f"{sum(1 << i for i in range(bits) if v[i] > 0):016x}"


# 标题尾部的站名 / 标签后缀，转载时各站各加各的
_TITLE_SUFFIX = re.compile(
    r"([_|｜\-—–]\s*[^_|｜\-—–]{1,14}\s*)+$")


def normalize_title(title: str) -> str:
    """标题归一化，用于 L2a 转载检测。

    实测发现：SimHash 算的是「标题+正文」，而同一篇稿件在不同站点的正文
    带各自的样板文字，指纹不同 —— 标题一模一样的转载反而漏掉了。
    故增加这一层：剥掉站名与标签后缀，去标点空白后精确比对。

      天猫上线「AI生意管家·新商版」…提升70%
      天猫上线「AI生意管家·新商版」…提升70%_凤凰网
      天猫上线「AI生意管家·新商版」…提升70%_号令天下
    三者归一化后一致，判为同一篇的三次转载。
    """
    s = _html.unescape(title)
    prev = None
    while prev != s:                       # 反复剥离，处理 `_新浪财经_新浪网` 多段后缀
        prev = s
        s = _TITLE_SUFFIX.sub("", s).strip()
    s = re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)
    return s.lower()


def hamming(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64


def strip_html(text: str) -> str:
    """去标签。先反转义实体——部分 feed（如第一财经）把 <b> 转义成 &lt;b&gt;
    塞进 title，不先 unescape 会留下孤立的 'b' 字母。"""
    s = _html.unescape(text)
    s = _html.unescape(s)                      # 二次转义的情况
    s = re.sub(r"(?is)<(script|style|noscript|nav|footer|header).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ============================================================ 时间口径
_ISO = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")


def parse_time(raw: str | None, *, exact: bool) -> tuple[str | None, str | None]:
    """返回 (ISO8601, time_source)。exact=True 表示来自结构化元数据。"""
    if not raw:
        return None, None
    raw = raw.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw.replace("GMT", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(), "exact" if exact else "parsed"
        except ValueError:
            continue
    m = _ISO.search(raw)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, tzinfo=timezone.utc).isoformat(), "parsed"
        except ValueError:
            pass
    return None, None


# ============================================================ Feed 解析
def _tag(block: str, *names: str) -> str | None:
    for n in names:
        m = re.search(rf"(?is)<{n}[^>]*>(.*?)</{n}>", block)
        if m:
            v = re.sub(r"(?s)<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1)).strip()
            if v:
                return v
        m = re.search(rf"(?is)<{n}[^>]*href=[\"']([^\"']+)[\"']", block)
        if m:
            return m.group(1).strip()
    return None


def parse_feed(xml: str, max_chars: int) -> list[dict]:
    """解析 RSS 2.0 / Atom，返回条目列表。"""
    blocks = re.findall(r"(?is)<item[\s>].*?</item>", xml) or \
             re.findall(r"(?is)<entry[\s>].*?</entry>", xml)
    out = []
    for b in blocks:
        link = _tag(b, "link", "guid")
        title = _tag(b, "title")
        if not link or not title or not link.startswith("http"):
            continue
        body = _tag(b, "content:encoded", "description", "summary", "content") or ""
        pub, tsrc = parse_time(_tag(b, "pubDate", "published", "updated", "dc:date"),
                               exact=True)
        out.append({
            "url": link, "title": strip_html(title)[:300],
            "content": strip_html(body)[:max_chars],
            "published_at": pub, "time_source": tsrc,
        })
    return out


# ============================================================ 通道实现
def collect_rss(src: dict, cfg: dict) -> tuple[list[dict], str | None]:
    st, body = fetch(src["channel_ref"])
    if not isinstance(st, int) or st != 200 or not body:
        return [], f"{src['id']}: HTTP {st}"
    return parse_feed(decode(body), cfg["search"]["content_max_chars"]), None


def collect_rsshub(src: dict, cfg: dict, instances: list[str]) -> tuple[list[dict], str | None]:
    last = None
    for inst in instances:
        st, body = fetch(inst.rstrip("/") + src["channel_ref"], timeout=25)
        if isinstance(st, int) and st == 200 and body:
            items = parse_feed(decode(body), cfg["search"]["content_max_chars"])
            if items:
                return items, None
        last = st
    return [], f"{src['id']}: 全部实例失败（末次 {last}）"


# 导航/栏目链接特征——直抓时必须排除，否则抓到的是菜单不是文章
_NAV_PATH = re.compile(
    r"(?i)/(about|contact|careers?|jobs?|privacy|terms|legal|sitemap|search|login|"
    r"register|rss|feed|tag|tags|category|categories|topic|topics|author|"
    r"channel|column|list|index|home|investor-relations|ir)(/|$|\.)")

# 文章 URL 特征：含日期路径 或 ≥5 位数字 ID
_ARTICLE_URL = re.compile(r"(/20\d{2}[-/]\d{1,2}|/\d{5,}|[-_/]\d{6,})")


def collect_direct(src: dict, cfg: dict) -> tuple[list[dict], str | None]:
    """直抓 SSR 页面，提取站内文章链接。

    必须区分文章链接与导航链接——早期版本无差别抓取，从 alibabagroup.com
    抓回来的全是「Investor Relations」「Culture and Values」这类菜单项。
    判据：站内域名 + 文章 URL 形态（日期或长数字 ID）+ 非导航路径 + 标题像标题。
    """
    st, body = fetch(src["channel_ref"])
    if not isinstance(st, int) or st != 200 or not body:
        return [], f"{src['id']}: HTTP {st}"
    page = decode(body)
    base = src["channel_ref"]
    pattern = src.get("article_url_pattern")
    art_re = re.compile(pattern) if pattern else _ARTICLE_URL

    seen, out = set(), []
    for m in re.finditer(r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page):
        href, text = m.group(1), strip_html(m.group(2))
        if len(text) < 10 or len(text) > 120:
            continue
        # 标题应含中文或足够多的词，纯英文短语多半是菜单
        if len(re.findall(r"[一-鿿]", text)) < 5 and len(text.split()) < 8:
            continue
        url = urllib.parse.urljoin(base, href)
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https"):
            continue
        if not any(d in p.netloc.lower() for d in src.get("domains", [])):
            continue
        if _NAV_PATH.search(p.path) or not art_re.search(p.path):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "title": text[:300], "content": "",
                    "published_at": None, "time_source": None})
        if len(out) >= 25:
            break
    return out, None


def collect_exa(cfg: dict, window_days: int) -> tuple[list[dict], dict, list[str]]:
    """Exa 搜索：公司×领域矩阵 + 无直采通道信源的站内定向检索。"""
    key = os.environ.get("EXA_API_KEY")
    if not key:
        return [], {"total": 0.0, "queries": 0}, ["EXA_API_KEY 未设置，跳过 Exa 通道"]

    s = cfg["search"]
    start = (datetime.now(timezone.utc).timestamp() - window_days * 86400)
    start_iso = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    tasks = [({"query": m["q"]}, m) for m in s["matrix"]]
    tasks += [({"query": q["q"], "includeDomains": q["domains"]}, None)
              for q in s.get("site_queries", [])]

    items, errors, cost, nq = [], [], 0.0, 0

    def one(extra: dict, meta: dict | None):
        payload = {
            "category": s["category"], "numResults": s["num_results"],
            "startPublishedDate": start_iso,
            "contents": {"text": {"maxCharacters": s["content_max_chars"]}},
            **extra,
        }
        req = urllib.request.Request(
            s["endpoint"], data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "x-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
                return json.loads(r.read()), extra["query"], meta, None
        except Exception as e:                                # noqa: BLE001
            return None, extra["query"], meta, f"Exa「{extra['query'][:20]}」: {type(e).__name__}"

    with ThreadPoolExecutor(max_workers=4) as ex:
        for f in as_completed([ex.submit(one, e, m) for e, m in tasks]):
            res, query, meta, err = f.result()
            nq += 1
            if err:
                errors.append(err)
                continue
            cost += float(res.get("costDollars", {}).get("total", 0) or 0)
            for r in res.get("results", []):
                if not r.get("url"):
                    continue
                pub, tsrc = parse_time(r.get("publishedDate"), exact=True)
                items.append({
                    "url": r["url"], "title": (r.get("title") or "")[:300],
                    "content": (r.get("text") or "")[:s["content_max_chars"]],
                    "published_at": pub, "time_source": tsrc,
                    "hint_company": (meta or {}).get("company"),
                    "hint_domain": (meta or {}).get("domain"),
                })
    return items, {"total": round(cost, 4), "queries": nq}, errors


# ============================================================ 信源归属
def build_domain_index(sources: list[dict]) -> dict[str, dict]:
    idx = {}
    for s in sources:
        for d in s.get("domains") or []:
            idx[d.lower()] = s
    return idx


def attribute(url: str, domain_idx: dict[str, dict]) -> dict | None:
    host = urllib.parse.urlsplit(url).netloc.lower()
    if host in domain_idx:
        return domain_idx[host]
    for d, s in domain_idx.items():
        if host == d or host.endswith("." + d):
            return s
    return None


def detect_original_source(text: str, patterns: dict[str, list[str]]) -> str | None:
    """原始出处归因（技术方案 §4.0）：从转载正文识别真实源头。"""
    head = text[:600]
    for sid, pats in patterns.items():
        if any(p in head for p in pats):
            return sid
    return None
