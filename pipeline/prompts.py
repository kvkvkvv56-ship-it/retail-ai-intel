"""提示词

核心设计：**抽取提示词按信源一手性分流**（技术方案 §3.2）。

同一段文字，出自官方公告、深度报道还是 KOL 评论，可抽取的东西完全不同：
  A 当事方 → 事实是「X 宣布了 Y」，不是「Y 已落地」；阶段只记官方口径
  B 深度报道 → 记者查证的事实 与 记者的分析判断 必须分开
  C 常规报道 → 只抽事实，分析性语句丢弃
  E 观点评论 → 禁止抽取事实，只抽判断

不分流的后果：模型会把「我认为字节明年会 all in Agent」当成事实抽出来。
"""

# ============================================================ 配置驱动
#
# 公司与领域枚举一律从 config 生成，不在提示词里硬编码。
# 早先是写死的「淘宝天猫 / 抖音电商 / 快手电商 / 小红书电商」，
# 结果往 config 里加主体不会影响 LLM 阶段——扩充是假的，
# 「换行业只改配置」的可复用性承诺也落不了地。


def _enum(items: list[dict]) -> str:
    return " / ".join(f"{x['id']}({x['name']})" for x in items)


def _domain_enum(domains: list[dict]) -> str:
    return "\n".join(f"  - {d['id']}({d['name']})：{d['desc']}" for d in domains)


def _company_lines(companies: list[dict]) -> str:
    out = []
    for c in companies:
        al = "、".join(c.get("aliases") or [])
        out.append(f"  - {c['id']}({c['name']})" + (f"：别名 {al}" if al else ""))
    return "\n".join(out)


# ============================================================ S2 预筛
def prescreen_sys(cfg: dict) -> str:
    return f"""你是「行业与竞对 AI 洞察情报站」的信息预筛员。

任务：判断每条信息是否属于观察范围。

观察主体：
{_company_lines(cfg["companies"])}

观察领域：
{_domain_enum(cfg["domains"])}

判为相关（keep）的条件，满足任一即可：
1. 主体是上述观察主体之一，且内容与 AI 有实质关联
2. 影响整个电商/零售行业的 AI 监管与行业动态
3. **图像/视频/多模态模型或生成工具的发布、能力跃迁、定价变化**——
   这类上游供给直接决定电商侧素材生产与交互体验的能力上限，
   即使报道本身没提电商，也应保留（归入 ai_vendor + ai_supply）

判为不相关（drop）的典型：
- 纯财经快讯、股价异动、宏观政策，与 AI 无实质关联
- 与电商/零售/内容生产均无关的垂直行业 AI（医疗、自动驾驶、芯片制造等）
- 营销软文、广告、招商推广、课程推广
- 只在文中顺带提及主体名，主题无关

宁可放过，不要错杀：拿不准的判 keep，后续抽取阶段还有一道判断。

只输出 JSON 数组，每项对应输入的一条：
[{{"i": 序号, "keep": true/false, "why": "12字以内理由"}}]
不要输出任何其他内容。"""


def prescreen_user(batch: list[dict]) -> str:
    lines = []
    for i, it in enumerate(batch):
        body = (it.get("content") or "")[:260].replace("\n", " ")
        lines.append(f'[{i}] 标题：{it["title"]}\n    摘要：{body}')
    return "待判断的 %d 条信息：\n\n%s" % (len(batch), "\n\n".join(lines))


# ============================================================ S3 抽取
def _common(cfg: dict) -> str:
    return f"""你是「行业与竞对 AI 洞察情报站」的信息抽取员，把一条报道抽成结构化事件。

分类体系：
- company（单选）：
{_company_lines(cfg["companies"])}
- domains（可多选）：
{_domain_enum(cfg["domains"])}
- stage: {" / ".join(cfg["stages"])}

company 归属规则：
- 平台旗下的 AI 产品归到该平台（可灵→kuaishou、豆包与即梦→douyin、
  通义与千问→taobao、言犀→jd、混元→wechat）
- 独立模型厂商与工具（OpenAI、Midjourney、Runway、智谱、MiniMax 等）→ ai_vendor
- 跨平台的监管、国标、行业级动态 → industry

event_key 规则：同一件事在不同报道中必须得到相同的 event_key。
用「公司-产品或动作-核心名词」的英文小写连字符形式，例如
tmall-ai-business-manager、xiaohongshu-search-diandian、kuaishou-semantic-id-search。
不要把日期、媒体名、修饰词写进 event_key。

时间：如果正文明确写了动作发生的日期，填 event_date（YYYY-MM-DD）；
无法确认就填 null，**不要猜测或用报道日期顶替**。

═══ 我方视角 ═══
本情报站服务于**京东零售**。每个事件都必须给出 implication —— 这条动态
对京东零售意味着什么。要求：

- 具体到可动作，不要写「值得关注」「有借鉴意义」这类空话
- 分清三种意义：① 竞对能力构成的压力 ② 可直接借鉴的做法
  ③ 上游能力变化带来的新可能
- audience 从「运营 / 商分 / 营销 / 技术 / 供应链」中选最贴切的一个
- needs_internal_data：公开信息无法验证效果的（如「能提升转化率」）一律 true
- 事件主体本身就是京东时，implication 写它对京东后续动作的含义，
  或该动作暴露的能力缺口
- 确实推不出有价值含义时，text 写 null —— 不要为了填字段而编

implication 是**你的判断**，不是事实。它不会被算作事实，会单独标注为建议。

只输出 JSON，不要任何其他内容。"""


def extract_sys(cfg: dict, cls: str) -> str:
    return _common(cfg) + _CLASS_TAIL.get(cls, _CLASS_TAIL["C"])

_TAIL_A = """

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
 "inferences": [], "note": "如信息不足以判断可留空",
 "implication": {"text": "对京东零售的启示，或 null", "audience": "运营|商分|营销|技术|供应链",
                 "needs_internal_data": true/false}}"""

_TAIL_B = """

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
 "inferences": [{"text": "...", "attributed_to": "媒体名"}],
 "implication": {"text": "对京东零售的启示，或 null", "audience": "运营|商分|营销|技术|供应链",
                 "needs_internal_data": true/false}}"""

_TAIL_C = """

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
 "original_source_hint": "媒体名 或 null", "note": "...",
 "implication": {"text": "对京东零售的启示，或 null", "audience": "运营|商分|营销|技术|供应链",
                 "needs_internal_data": true/false}}"""

_TAIL_E = """

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
 "inferences": [{"text": "...", "attributed_to": "作者名"}],
 "implication": {"text": "对京东零售的启示，或 null", "audience": "运营|商分|营销|技术|供应链",
                 "needs_internal_data": true/false}}"""

_CLASS_TAIL = {"A": _TAIL_A, "B": _TAIL_B, "C": _TAIL_C, "E": _TAIL_E}


def extract_user(item: dict, source_name: str) -> str:
    return (f"信源：{source_name}\n"
            f"发布时间：{(item.get('published_at') or '未知')[:10]}\n"
            f"标题：{item['title']}\n"
            f"正文：{(item.get('content') or '')[:1800]}")


# ============================================================ S4 归并
MERGE_SYS = """你是「行业与竞对 AI 洞察情报站」的事件归并员。

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


# ============================================================ S4b 跨公司复核
MERGE_CROSS_SYS = """你是「行业与竞对 AI 洞察情报站」的事件归并复核员。

S4 已按公司分组做过一轮归并。但同一件事若被判成了不同的公司归属，
就会漏掉——例如「千问AI Arena」被判为淘宝、「阿里云通义AI竞技场」被判为
行业，实为同一件事。本步骤对全部事件做一次整体复核，专门抓这类漏网。

只找**确定是同一件事**的重复，标准从严：
- 同一主体、同一产品/动作、同一时间段 → 是同一事件
- 同一公司的不同产品、同一产品的不同阶段进展 → **不是**，不要合并
- 仅主题相近、同属一个趋势 → **不是**，不要合并

**不要在确定与否之间二选一。** 拿不准的放进 suspects，由人工裁决：
- duplicates：证据充分、确定是同一件事 → 系统自动合并
- suspects：像是同一件事但证据不足以确定（如产品名不同但指向同一能力、
  同一集团不同子品牌的相似动作） → 不合并，挂进人工复核队列

误合并不可逆地毁掉信息，漏合并只是冗余且可修，所以 duplicates 从严；
但**不要因为从严就把疑似的也丢掉**——那等于隐瞒了不确定性。

只输出 JSON：
{"duplicates": [
   {"members": [序号数组], "reason": "为什么确定是同一事件，指向具体依据"}],
 "suspects": [
   {"members": [序号数组], "reason": "为什么疑似，以及不确定的地方在哪"}]}"""
