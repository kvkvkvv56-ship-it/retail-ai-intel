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
import threading
import time
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


# --------------------------------------------------------------- jina 通道
JINA = "https://r.jina.ai/"

# Jina 返回的是 markdown，URL 后面常紧跟 ) ] " ' < > 等收尾符，需一并排除
URL_RE = re.compile(r"""(https?://[^\s)\]"'<>]+)""")

# Jina 免费额度 20 RPM（无 API key）。并发打请求会撞 403 —— 实测一次
# 「列表页 + 8 篇文章 × 2 个信源」的突发就足以触发。故串行 + 固定间隔，
# 撞到 403 退避后重试一次。慢，但每天只跑两轮，不构成瓶颈。
# 实测结论：**匿名调用不可用于生产**。无 API key 时 Jina 按 IP 限流，
# 一天内做几十次探测就会把配额打光，之后固定 403 —— 同样的请求头
# 几分钟前 200、之后一直 403，与请求构造无关。
# 免费 API key（jina.ai 注册即得）额度 200 RPM，足够本项目每天两轮。
# 故：无 key 直接跳过该通道，不浪费 25 秒去撞 403。
_JINA_MIN_INTERVAL = 0.4          # 有 key 时 200 RPM，0.4s 间隔留足余量
_jina_last = [0.0]
_jina_lock = threading.Lock()


def jina_enabled() -> bool:
    return bool(os.environ.get("JINA_API_KEY"))


def _jina_get(url: str, headers: dict | None = None):
    """限流版 Jina 请求：全局串行 + 403 退避重试。"""
    key = os.environ.get("JINA_API_KEY")
    headers = {**(headers or {})}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for attempt in range(2):
        with _jina_lock:
            gap = time.monotonic() - _jina_last[0]
            if gap < _JINA_MIN_INTERVAL:
                time.sleep(_JINA_MIN_INTERVAL - gap)
            _jina_last[0] = time.monotonic()
        st, body = fetch(url, timeout=70, headers=headers)
        if st == 403 and attempt == 0:
            time.sleep(20)            # 撞限流，退避后再试一次
            continue
        return st, body
    return st, body


def collect_jina(src: dict, cfg: dict) -> tuple[list[dict], str | None]:
    """经 Jina Reader 采集客户端渲染的站点。

    晚点 LatePost 与亿邦动力是本项目最想要、却三条通道全堵的两个信源：
    前端渲染直抓不到、无 RSSHub 路由、不在 wechat2rss 免费列表。
    Jina Reader 在服务端渲染 JS 后返回 markdown，实测：
      亿邦首页 直抓 0 中文字 → Jina 3498 字
      晚点文章页 直抓 3 字   → Jina 11288 字（全文）

    两个已知限制，都不阻断可用性：
      1. 列表页需带 X-With-Links-Summary 头才吐链接（不带则只有正文）
      2. 两站都不给发布时间；亿邦文章页还会 403（Jina 出口 IP 被封）
         → published_at 留空，交由 S3 从正文抽 event_date，
           S5 的状态判定本就以 event_date 为准（§6.4）
    """
    if not jina_enabled():
        return [], (f"{src['id']}: 未设置 JINA_API_KEY，跳过 Jina 通道"
                    "（匿名额度不足以支撑生产采集，实测会稳定 403）")

    st, body = _jina_get(src["channel_ref"],
                         headers={"X-With-Links-Summary": "true"})
    if not isinstance(st, int) or st != 200 or not body:
        return [], f"{src['id']}: 列表页 Jina {st}"

    page = decode(body)
    pattern = src.get("article_url_pattern")
    if not pattern:
        return [], f"{src['id']}: 缺少 article_url_pattern，无法识别文章链接"
    art_re = re.compile(pattern)

    # 「标题 + 链接」配对（Jina 的 links summary 用 markdown 链接格式）
    pairs, seen = [], set()
    for title, u in re.findall(r"- \[([^\]]{4,90})\]\((https?://[^)]+)\)", page):
        u = u.rstrip(".,;")
        if art_re.search(u) and u not in seen:
            seen.add(u)
            pairs.append((strip_html(title), u))
    # 配对失败时退回纯 URL 提取（拿不到标题，只能盲抓）
    if not pairs:
        for u in URL_RE.findall(page):
            u = u.rstrip(".,;")
            if art_re.search(u) and u not in seen:
                seen.add(u)
                pairs.append(("", u))
    if not pairs:
        return [], f"{src['id']}: 列表页未匹配到文章链接"

    # 仅列表模式：文章页取不到时的降级。亿邦动力即属此类——列表页与文章页
    # 都是前端渲染，且文章页对 Jina 出口 IP 返回 403。但列表的链接文字就是
    # 标题、URL 路径里带日期（/YYYYMMDD/），足以生成可追溯的条目。
    # 没有正文，抽取会偏薄；其真正价值在于**作为独立信源为其他事件提供
    # 多源印证**——置信度计算只看独立组织数，不要求每个源都有正文。
    if src.get("jina_listing_only"):
        date_re = re.compile(src.get("url_date_pattern", r"/(20\d{2})(\d{2})(\d{2})/"))
        out, seen2 = [], set()
        for title, u in re.findall(r"- \[([^\]]{4,90})\]\((https?://[^)]+)\)", page):
            if not art_re.search(u) or u in seen2:
                continue
            seen2.add(u)
            m = date_re.search(u)
            pub, tsrc = (None, None)
            if m:
                pub, tsrc = parse_time("-".join(m.groups()), exact=False)
            out.append({"url": u, "title": strip_html(title)[:300], "content": "",
                        "published_at": pub, "time_source": tsrc})
        if not out:
            return [], f"{src['id']}: 列表页未解析出「标题+链接」配对"
        return out[:int(src.get("jina_article_limit", 25))], None

    limit = int(src.get("jina_article_limit", 8))

    # 标题级粗筛：只对可能相关的文章抓全文。
    #
    # 晚点覆盖机器人 / 汽车 / 芯片 / 云 / 电商全赛道，盲抓最新 8 篇命中电商的
    # 概率很低——首轮 16 篇里一篇电商都没有，全被后续粗筛与预筛拦掉，
    # 白花了 16 次 Jina 请求。改为先看标题、只抓命中的。
    #
    # 判据只用 industry_terms（公司名 / 电商零售词），不要求标题里出现 AI：
    # 标题短，「阿里妈妈万相点睛」这类不带 AI 字样但确属观察范围；
    # 反之只带 AI 却讲汽车的，行业词过不了。AI 相关性留给正文阶段判。
    ind = (cfg.get("relevance") or {}).get("industry_terms") or []
    titled = [(t, u) for t, u in pairs if t]
    dropped: list[dict] = []
    if ind and titled:
        hit, miss = [], []
        for t, u in titled:
            (hit if any(k in t for k in ind) else miss).append((t, u))
        picked = hit
        # 被标题粗筛拦下的条目一律进拒绝台账 —— 不丢弃只记账（§5.2）。
        #
        # 早先这里有个「一条不中就退回取最新 N 篇」的兜底，是错的：晚点近期
        # 全是机器人/医疗/半导体选题，一条行业词都不命中，兜底却照样抓了 6 篇
        # 全文，既白花请求又把不相关内容灌进流水线，而且兜底路径不记账，
        # 导致这一层完全不可审计。
        #
        # 现在的行为：一条不中就本轮不产出。这是诚实的结果——该信源本期确实
        # 没有观察范围内的内容。全部标题仍写入台账，可据此判断筛得是否过严。
        dropped = [{"url": u, "title": t,
                    "reason": "标题未命中行业相关词，未抓取全文（省一次全文渲染请求）"}
                   for t, u in miss]
    else:
        picked = pairs[:limit]

    out, errs = [], 0

    def one(u: str):
        st2, b2 = _jina_get(JINA + u)
        if not isinstance(st2, int) or st2 != 200 or not b2:
            return None
        t = decode(b2)
        title = re.search(r"^Title: (.+)$", t, re.M)
        if not title or "403" in title.group(1) or "Forbidden" in title.group(1):
            return None
        content = t.split("Markdown Content:", 1)[-1]
        content = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", content)   # 去图片
        content = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"", content)  # 链接留文字
        content = re.sub(r"\s+", " ", content).strip()
        if len(re.findall(r"[一-鿿]", content)) < 120:
            return None
        pub, tsrc = parse_time(
            (re.search(r"Published Time: (.+)", t) or [None, None])[1]
            if re.search(r"Published Time: (.+)", t) else None, exact=True)
        return {"url": u, "title": title.group(1).strip()[:300],
                "content": content[:cfg["search"]["content_max_chars"]],
                "published_at": pub, "time_source": tsrc}

    for _t, u in picked[:limit]:
        r = one(u)
        if r:
            out.append(r)
        else:
            errs += 1

    # 把被标题粗筛拦下的挂在返回值上，由 run.py 写入拒绝台账
    for d in dropped:
        out.append({"url": d["url"], "title": d["title"], "content": "",
                    "published_at": None, "time_source": None,
                    "_prefilter_drop": d["reason"]})

    notes = []
    if errs:
        notes.append(f"{errs}/{min(len(picked), limit)} 篇取回失败")
    if dropped:
        notes.append(f"标题粗筛跳过 {len(dropped)} 篇（省下同等次数的全文抓取）")
    return out, (f"{src['id']}: " + "；".join(notes)) if notes else None


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
