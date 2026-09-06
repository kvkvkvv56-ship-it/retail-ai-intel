"""提示词

核心设计：**抽取提示词按信源一手性分流**（技术方案 §3.2）。

同一段文字，出自官方公告、深度报道还是 KOL 评论，可抽取的东西完全不同：
  A 当事方 → 事实是「X 宣布了 Y」，不是「Y 已落地」；阶段只记官方口径
  B 深度报道 → 记者查证的事实 与 记者的分析判断 必须分开
  C 常规报道 → 只抽事实，分析性语句丢弃
  E 观点评论 → 禁止抽取事实，只抽判断

不分流的后果：模型会把「我认为字节明年会 all in Agent」当成事实抽出来。
"""

# ============================================================ S2 预筛
PRESCREEN_SYS = """你是「行业与竞对 AI 洞察助手」的信息预筛员。

任务：判断每条信息是否属于观察范围。观察范围是——
中国电商平台（淘宝天猫 / 抖音电商 / 快手电商 / 小红书电商）及行业监管，
在 **AI 相关** 的产品、功能、工具、运营动作、技术路径、组织信号上的动态。

判为相关（keep）的条件（需同时满足）：
1. 主体是上述电商平台之一，或是影响整个电商行业的监管/行业动态
2. 内容与 AI 有实质关联——不是仅仅提到「AI」这个词

判为不相关（drop）的典型：
- 纯财经快讯、股价异动、宏观政策，与 AI 无实质关联
- 通用 AI 技术新闻（模型发布、论文、芯片），未落到电商/零售场景
- 营销软文、广告、招商推广、课程推广
- 只在文中顺带提及平台名，主题无关

只输出 JSON 数组，每项对应输入的一条：
[{"i": 序号, "keep": true/false, "why": "12字以内理由"}]
不要输出任何其他内容。"""


def prescreen_user(batch: list[dict]) -> str:
    lines = []
    for i, it in enumerate(batch):
        body = (it.get("content") or "")[:260].replace("\n", " ")
        lines.append(f'[{i}] 标题：{it["title"]}\n    摘要：{body}')
    return "待判断的 %d 条信息：\n\n%s" % (len(batch), "\n\n".join(lines))


# ============================================================ S3 抽取
_COMMON = """你是「行业与竞对 AI 洞察助手」的信息抽取员，把一条报道抽成结构化事件。

分类体系：
- company: taobao(淘宝天猫) / douyin(抖音电商) / kuaishou(快手电商) /
           xiaohongshu(小红书电商) / industry(行业·监管)
- domains: retail_ops(商品与零售运营) / marketing(营销与内容生成) /
           merchant_tools(商家工具与服务) / data_bi(数据与商业分析) /
           service_fulfillment(客服与履约)，可多选
- stage: 概念宣传 / 试点探索 / 已上线 / 规模化应用

event_key 规则：同一件事在不同报道中必须得到相同的 event_key。
用「公司-产品或动作-核心名词」的英文小写连字符形式，例如
tmall-ai-business-manager、xiaohongshu-search-diandian、kuaishou-semantic-id-search。
不要把日期、媒体名、修饰词写进 event_key。

时间：如果正文明确写了动作发生的日期，填 event_date（YYYY-MM-DD）；
无法确认就填 null，**不要猜测或用报道日期顶替**。

只输出 JSON，不要任何其他内容。"""

EXTRACT_A = _COMMON + """

本条信息来自【当事方官方渠道】（公告 / 财报 / 官网 / 官方账号）。

特别约束：
1. 官方口径的事实是「**该公司宣布了什么**」，不是「该事已经落地生效」。
   facts 的表述必须体现这一点，例如「淘宝宣布上线 AI 生意管家新商版」，
   而不是「淘宝的 AI 生意管家已服务 X 万商家」——除非官方明确给出了该数字。
2. stage 只反映**官方自述**的阶段。官方常把试点说成上线，这是已知偏差，
   后续会由第三方报道交叉打折，你不需要自己打折，如实记录即可。
3. 官方通稿里的营销话术（「全面赋能」「重磅升级」「行业首创」）不是事实，丢弃。

输出：
{"relevant": true/false, "company": "...", "domains": [...],
 "event_key": "...", "event_title": "20字以内的中性标题",
 "summary": "50字以内客观概述", "stage": "...",
 "facts": ["每条一个可核查的事实，必须来自本文"],
 "inferences": [], "note": "如信息不足以判断可留空"}"""

EXTRACT_B = _COMMON + """

本条信息来自【原创深度报道】（有独立采访 / 内部信源 / 调查）。

特别约束：**必须把「记者查证的事实」与「记者的分析判断」分开。**
- facts：记者报道的可核查事实（谁在什么时候做了什么、具体数字、具体功能）
- inferences：记者的分析、预测、评价、归因推测
  每条填 attributed_to = 媒体名（如「晚点LatePost」「财新」），
  表明这是**该媒体的判断**，不是既成事实。

判断标志词：「或将」「预计」「业内人士认为」「记者了解到...可能」
「这意味着」「背后逻辑是」——这些引导的内容属于 inferences。

输出：
{"relevant": true/false, "company": "...", "domains": [...],
 "event_key": "...", "event_title": "...", "summary": "...", "stage": "...",
 "event_date": "YYYY-MM-DD 或 null",
 "facts": ["..."],
 "inferences": [{"text": "...", "attributed_to": "媒体名"}]}"""

EXTRACT_C = _COMMON + """

本条信息来自【常规媒体报道】（基于公开信息的日常报道、快讯）。

特别约束：
1. **只抽事实**。这类报道多为二手整理，其中的分析评论价值有限，一律丢弃，
   不要放进 inferences。
2. 如果正文标注了原始出处（「据晚点LatePost报道」「来源：财新」），
   填入 original_source_hint 字段——这条信息的真正源头不是本媒体。
3. 如果整篇只是转述另一家的报道且无增量信息，relevant 仍可为 true，
   但在 note 中注明「疑似转载」。

输出：
{"relevant": true/false, "company": "...", "domains": [...],
 "event_key": "...", "event_title": "...", "summary": "...", "stage": "...",
 "event_date": "YYYY-MM-DD 或 null",
 "facts": ["..."], "inferences": [],
 "original_source_hint": "媒体名 或 null", "note": "..."}"""

EXTRACT_E = _COMMON + """

本条信息来自【个人观点 / KOL 评论】。

特别约束：**禁止抽取 facts，facts 必须为空数组。**
这类内容的价值在于「行业内的人怎么解读」，不在于「发生了什么」。
作者陈述的所谓事实未经独立核实，不能进入知识库作为事实依据。

只抽 inferences：作者的判断、预测、评价、对趋势的解读。
每条填 attributed_to = 作者/账号名。

输出：
{"relevant": true/false, "company": "...", "domains": [...],
 "event_key": "...", "event_title": "...", "summary": "...",
 "stage": null, "facts": [],
 "inferences": [{"text": "...", "attributed_to": "作者名"}]}"""

EXTRACT_BY_CLASS = {"A": EXTRACT_A, "B": EXTRACT_B, "C": EXTRACT_C, "E": EXTRACT_E}


def extract_user(item: dict, source_name: str) -> str:
    return (f"信源：{source_name}\n"
            f"发布时间：{(item.get('published_at') or '未知')[:10]}\n"
            f"标题：{item['title']}\n"
            f"正文：{(item.get('content') or '')[:1800]}")


# ============================================================ S4 归并
MERGE_SYS = """你是「行业与竞对 AI 洞察助手」的事件归并员。

给你同一家公司的一组候选条目，每条已由前一步抽出 event_key 与标题。
任务：判断哪些条目描述的是**同一个事件**，把它们归为一组。

判定标准：
- 同一事件 = 同一主体在同一时间段做的同一件事。不同报道角度、不同标题、
  不同媒体，只要说的是同一件事，就归为一组。
- **不要因为公司相同就合并**。「天猫上线AI空间站」与「天猫上线AI生意管家」
  是两个不同的产品动作，必须分开。
- 同一产品的不同阶段进展（发布 → 扩量 → 数据披露）算不同事件，但要在
  relation 中标注 follows 关系。

每组必须给出归并理由，理由要指向具体依据（同一产品名、同一发布动作等），
不能只写「内容相似」。

只输出 JSON：
{"groups": [
   {"canonical_key": "统一后的 event_key",
    "title": "该事件的中性标题",
    "members": [条目序号数组],
    "reason": "为什么这些是同一事件，指向具体依据"}],
 "relations": [
   {"from": "event_key", "to": "event_key", "relation": "follows",
    "basis": "依据"}]}"""


def merge_user(company_name: str, cands: list[dict]) -> str:
    lines = []
    for i, c in enumerate(cands):
        lines.append(f'[{i}] key={c["event_key"]}\n'
                     f'    标题：{c["event_title"]}\n'
                     f'    概述：{(c.get("summary") or "")[:110]}')
    return f"公司：{company_name}\n候选条目 {len(cands)} 条：\n\n" + "\n\n".join(lines)
