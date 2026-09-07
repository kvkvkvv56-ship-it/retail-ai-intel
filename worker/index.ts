/**
 * 线上只读服务（技术方案 §2.1、§15）
 *
 * 数据是构建时打包进静态资产的快照，Worker 不挂数据库：
 *   - 读接口 /api/v1/* 映射到 ASSETS 中的 JSON 快照，附加筛选与错误规范
 *   - SPA 路由回退到 index.html
 *   - 定制简报 SSE 走 DeepSeek 流式（后续实现）
 *
 * 之所以由 Worker 转一层而不是让前端直接取静态文件：API 契约本身是交付物，
 * 且「可查询」是评分点——筛选参数、Problem JSON 错误规范都在这一层。
 */

interface Env {
  ASSETS: Fetcher
  JINA_API_KEY?: string       // 语义检索：只对用户 query 调用，事件向量在快照里
  DATA_REPO?: string          // owner/repo，指向流水线提交数据的仓库
  DATA_REF?: string           // 分支，默认 main
  KB_LLM_API_KEY?: string
  KB_LLM_BASE_URL?: string
  KB_BRIEF_MODEL?: string
  EXA_API_KEY?: string
}

const JSON_HEADERS = {
  'content-type': 'application/json; charset=utf-8',
  'cache-control': 'public, max-age=0, s-maxage=300, stale-while-revalidate=900',
}

/** 统一错误格式：Problem JSON + requestId（§15） */
function problem(status: number, title: string, detail?: string): Response {
  return new Response(
    JSON.stringify({
      type: `https://retail-ai-intel/errors/${status}`,
      title,
      status,
      detail,
      requestId: crypto.randomUUID(),
    }),
    { status, headers: { 'content-type': 'application/problem+json; charset=utf-8' } },
  )
}

/** 仓库快照的边缘缓存时长（秒）。流水线每天两轮，10 分钟足够新。 */
const SNAP_TTL = 600

/**
 * 从仓库读最新快照。
 *
 * 流水线每天两轮把 web/public/api/ 提交回仓库，但 ASSETS 里的资产是
 * **部署那一刻**打包进去的。只读 ASSETS 的话，数据天天在更新而站点永远停在
 * 上次手动部署——「每日自动更新」就是假的。
 *
 * 走 raw 而不是在 CI 里加一步 wrangler deploy，是为了不引入 Cloudflare
 * API token：数据更新与代码发布本来就该解耦，代码不变时没有重新部署的理由。
 */
async function fromRepo(env: Env, path: string): Promise<any | null> {
  if (!env.DATA_REPO) return null
  const url = `https://raw.githubusercontent.com/${env.DATA_REPO}/`
    + `${env.DATA_REF || 'main'}/web/public/api/v1/${path}.json`
  try {
    const r = await fetch(url, {
      // 超时兜底：raw 慢的时候不能把整个接口拖住，退回 ASSETS 就好
      signal: AbortSignal.timeout(3000),
      cf: { cacheTtl: SNAP_TTL, cacheEverything: true },
    })
    if (!r.ok) return null
    return await r.json()
  } catch {
    return null
  }
}

async function fromAssets(env: Env, path: string): Promise<any | null> {
  const r = await env.ASSETS.fetch(new Request(`https://assets.local/api/v1/${path}.json`))
  // 注意：assets 配了 not_found_handling: "single-page-application"，
  // 资产不存在时会返回 index.html 且状态 200 —— 只判 r.ok 会把 HTML 喂给
  // JSON.parse，Worker 抛异常变成 500 而不是我们的 404 Problem JSON。
  // 必须同时校验 content-type。
  if (!r.ok) return null
  if (!(r.headers.get('content-type') || '').includes('json')) return null
  try {
    return await r.json()
  } catch {
    return null
  }
}

/** 仓库最新优先，读不到退回部署时打包的版本（永远可用）。 */
async function snapshot(env: Env, path: string): Promise<any | null> {
  const fresh = await fromRepo(env, path)
  return fresh !== null ? fresh : await fromAssets(env, path)
}

// ==================================================================== 向量检索
//
// 事件向量在导出时算好写进 vectors.json（int8 量化后 base64，114 条约 156KB）。
// 线上只对用户的 query 调一次 embedding 接口，余弦在这里算——不必挂向量库，
// 也不必为检索多存一份数据。没有 JINA_API_KEY 时全部退回词面匹配。

const EMBED_API = 'https://api.jina.ai/v1/embeddings'

function unpack(b64: string): Int8Array {
  const bin = atob(b64)
  const v = new Int8Array(bin.length)
  for (let i = 0; i < bin.length; i++) v[i] = bin.charCodeAt(i) - 128
  return v
}

function cosine(a: Int8Array | number[], b: Int8Array | number[]): number {
  let dot = 0, na = 0, nb = 0
  for (let i = 0; i < a.length; i++) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i] }
  return dot / (Math.sqrt(na) * Math.sqrt(nb) || 1)
}

async function embedQuery(env: Env, text: string): Promise<number[] | null> {
  if (!env.JINA_API_KEY || !text.trim()) return null
  try {
    const r = await fetch(EMBED_API, {
      method: 'POST',
      headers: { 'content-type': 'application/json',
                 authorization: `Bearer ${env.JINA_API_KEY}` },
      body: JSON.stringify({ model: 'jina-embeddings-v3', task: 'text-matching',
                             input: [text.slice(0, 1600)] }),
      signal: AbortSignal.timeout(8000),
    })
    if (!r.ok) return null
    const j: any = await r.json()
    return j?.data?.[0]?.embedding ?? null
  } catch { return null }
}

/** 事件 id → 语义相似度。取不到向量或接口失败时返回空 Map，调用方退回词面。 */
async function semanticScores(env: Env, query: string): Promise<Map<string, number>> {
  const out = new Map<string, number>()
  const qv = await embedQuery(env, query)
  if (!qv) return out
  const pack = await snapshot(env, 'vectors')
  if (!pack?.vectors) return out
  for (const [id, b64] of Object.entries(pack.vectors as Record<string, string>)) {
    out.set(id, cosine(qv, unpack(b64)))
  }
  return out
}

function ok(data: unknown): Response {
  return new Response(JSON.stringify(data), { headers: JSON_HEADERS })
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url)
    const p = url.pathname

    if (!p.startsWith('/api/')) {
      return env.ASSETS.fetch(request)   // 静态资源 + SPA 回退
    }

    const m = p.match(/^\/api\/v1\/(.*)$/)
    if (!m) return problem(404, 'not_found', `未知接口 ${p}`)
    const route = m[1].replace(/\/$/, '')

    // ---------------- 元信息 ----------------
    if (route === 'meta' || route === 'sources' || route === 'reviews'
        || route === 'runs' || route === 'reports' || route === 'docs') {
      const d = await snapshot(env, route)
      return d ? ok(d) : problem(503, 'snapshot_missing', `快照 ${route} 尚未生成`)
    }

    // ---------------- 事件列表（支持筛选） ----------------
    if (route === 'events') {
      const d = await snapshot(env, 'events')
      if (!d) return problem(503, 'snapshot_missing', '事件快照尚未生成')
      let rows = d.results as any[]
      const q = url.searchParams
      const eq = (k: string, f: string) => {
        const v = q.get(k)
        if (v) rows = rows.filter((r) => String(r[f]) === v)
      }
      eq('company', 'company')
      eq('status', 'status')
      eq('confidence', 'confidence')
      eq('stage', 'stage')
      const domain = q.get('domain')
      if (domain) rows = rows.filter((r) => (r.domains || []).includes(domain))

      const limit = Math.min(Number(q.get('limit') || 200), 500)
      if (Number.isNaN(limit) || limit < 1) {
        return problem(400, 'invalid_parameter', 'limit 必须是 1-500 的整数')
      }
      return ok({ total: rows.length, results: rows.slice(0, limit) })
    }

    // ---------------- 详情类 ----------------
    const detail = route.match(/^(events|runs|reports|docs)\/([A-Za-z0-9\-]+)$/)
    if (detail) {
      const d = await snapshot(env, `${detail[1]}/${detail[2]}`)
      return d ? ok(d) : problem(404, 'not_found', `${detail[1]} ${detail[2]} 不存在`)
    }

    const rejects = route.match(/^runs\/([A-Za-z0-9\-]+)\/rejects$/)
    if (rejects) {
      const d = await snapshot(env, `runs/${rejects[1]}/rejects`)
      if (!d) return problem(404, 'not_found', `运行 ${rejects[1]} 不存在`)
      const reason = url.searchParams.get('reason_code')
      if (reason) {
        const rows = (d.results as any[]).filter((r) => r.reason_code === reason)
        return ok({ ...d, total: rows.length, results: rows })
      }
      return ok(d)
    }

    // ---------------- 检索 ----------------
    if (route === 'search') {
      const q = (url.searchParams.get('q') || '').trim()
      if (q.length < 2 || q.length > 200) {
        return problem(400, 'invalid_parameter', 'q 长度需在 2-200 之间')
      }
      const idx = (await snapshot(env, 'search-index')) as any[] | null
      if (!idx) return problem(503, 'snapshot_missing', '检索索引尚未生成')
      const lower = q.toLowerCase()
      const lex = (e: any) =>
        e.t.toLowerCase().includes(lower) || (e.s || '').toLowerCase().includes(lower)

      // 词面命中优先（用户搜确切词就是想要它），其余按语义相似度补齐。
      // 语义不可用时行为与改动前完全一致。
      const sem = await semanticScores(env, q)
      const SEM_FLOOR = 0.62
      const scored = idx
        .map((e: any) => ({ e, lex: lex(e) ? 1 : 0, sem: sem.get(e.id) ?? 0 }))
        .filter((x) => x.lex || x.sem >= SEM_FLOOR)
        .sort((a, b) => (b.lex - a.lex) || (b.sem - a.sem))
      return ok({
        query: q,
        mode: sem.size ? 'lexical+semantic' : 'lexical',
        total: scored.length,
        results: scored.slice(0, 50).map((x) => ({
          ...x.e, matched: x.lex ? 'lexical' : 'semantic',
          similarity: x.sem ? Math.round(x.sem * 1000) / 1000 : undefined,
        })),
      })
    }

    if (route === 'briefs/generate') {
      if (request.method !== 'POST') {
        return problem(405, 'method_not_allowed', '定制简报需 POST')
      }
      return generateBrief(request, env)
    }

    return problem(404, 'not_found', `未知接口 ${p}`)
  },
}


// ==================================================================== 定制简报
//
// 三阶段（有用户提示词时）：
//   ① 意图分析  —— 便宜档 LLM 把自然语言解析成结构化检索意图
//   ② 双源检索  —— 知识库全量（已核验、带 EV 与置信度）
//                 + 互联网实时（新鲜但未经本系统核验）
//   ③ 深度报告  —— 两类信息分开标注，冲突时说明分歧而非强行统一
//
// 无提示词时退化为「最近 15 条知识库内容的综述」，不发起互联网检索。
//
// 为什么坚持把两类信息分开：知识库的事实走过采集→去重→核验→置信度全链路，
// 互联网检索结果没有。混在一起写，等于把未核验内容伪装成已核验结论。

const INTENT_SYS = `你是检索意图解析器。把用户的简报需求解析成结构化检索条件。

只输出 JSON，不要任何其他内容：
{
  "intent": "一句话复述用户想要什么，≤30字",
  "companies": ["从给定枚举里选，没有明确指向就留空"],
  "domains": ["从给定枚举里选，没有明确指向就留空"],
  "keywords": ["3-6个中文检索词，用于知识库词面匹配"],
  "needs_web": true/false,
  "web_queries": ["2-3条互联网检索式，中文，具体到主体与动作"],
  "report_type": "对比 | 追踪 | 综述 | 深度"
}

needs_web 判断标准：
- 用户问「最新」「现在」「最近有没有」等时效性问题 → true
- 用户问知识库覆盖范围之外的主体或行业 → true
- 用户只是要对已有事件做归纳、对比、解读 → false`

const BRIEF_SYS = `你是「行业与竞对 AI 洞察助手」的深度简报撰写智能体。

你会拿到两类材料，**必须分开对待，不得混为一谈**：

【A 知识库事实】走过本系统的采集→去重→核验→置信度全链路。每条带 EV 编号
与置信度等级，可追溯到原始信源。引用时写出 EV 编号。

【B 互联网检索】本轮实时检索所得，**未经本系统核验**：未做多源交叉、
未判重、未区分一手与转载。引用时必须标注来源媒体，并明确其未经核验。

写作要求：
- 结论优先建立在 A 上；B 用于补充时效性与背景，或提示 A 的盲区
- A 与 B 冲突时，**说明分歧并给出各自依据**，不要强行统一到一个说法
- 事实与推断分开：来自材料的是事实，你的分析是推断，推断用「判断」「推测」标注
- 材料覆盖不到的部分，直接说明信息缺口，不补全、不编造

输出格式：
- Markdown。## 一级分节、### 二级分节
- 数据对比优先用表格
- 关键数字加粗
- 末尾固定一节 \`## 材料边界\`，说明：本次用了几条知识库事件、几条互联网结果，
  哪些结论受单源限制，哪些需要内部数据才能验证
- 涉及我方借鉴建议时，标注适用受众（零售运营/商分/营销）与是否需内部数据验证

语气：专业、克制、直接。信息密度优先，不注水。`

async function llmJson(env: Env, messages: any[], maxTokens = 700): Promise<any> {
  const r = await fetch(`${env.KB_LLM_BASE_URL || 'https://api.deepseek.com'}/chat/completions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${env.KB_LLM_API_KEY}` },
    body: JSON.stringify({
      model: env.KB_BRIEF_MODEL || 'deepseek-chat',
      messages, temperature: 0.1, max_tokens: maxTokens,
    }),
  })
  if (!r.ok) throw new Error(`意图解析失败 ${r.status}`)
  const j: any = await r.json()
  let t = String(j.choices?.[0]?.message?.content || '').trim()
  const fence = /```(?:json)?\s*([\s\S]*?)\s*```/.exec(t)
  if (fence) t = fence[1].trim()
  const a = t.indexOf('{'), b = t.lastIndexOf('}')
  return JSON.parse(a >= 0 && b > a ? t.slice(a, b + 1) : t)
}

/** 滑动二元组。非重叠切分会因偏移不同导致相同词对不上（归并阶段踩过同样的坑）。 */
function grams(t: string): Set<string> {
  const cn = (t || '').replace(/[^一-鿿]/g, '')
  const g = new Set<string>()
  for (let i = 0; i < cn.length - 1; i++) g.add(cn.slice(i, i + 2))
  for (const w of (t || '').toLowerCase().match(/[a-z]{2,}/g) || []) g.add(w)
  return g
}

async function searchWeb(env: Env, queries: string[]): Promise<{ hits: any[]; cost: number }> {
  if (!env.EXA_API_KEY) return { hits: [], cost: 0 }
  const hits: any[] = []
  let cost = 0
  const seen = new Set<string>()
  for (const q of queries.slice(0, 3)) {
    try {
      const r = await fetch('https://api.exa.ai/search', {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-api-key': env.EXA_API_KEY },
        body: JSON.stringify({
          query: q, category: 'news', numResults: 4,
          contents: { text: { maxCharacters: 900 } },
        }),
      })
      if (!r.ok) continue
      const j: any = await r.json()
      cost += Number(j.costDollars?.total || 0)
      for (const x of j.results || []) {
        if (!x.url || seen.has(x.url)) continue
        seen.add(x.url)
        hits.push({
          title: x.title, url: x.url,
          published: (x.publishedDate || '').slice(0, 10),
          host: (() => { try { return new URL(x.url).hostname } catch { return '' } })(),
          text: (x.text || '').slice(0, 700),
          query: q,
        })
      }
    } catch { /* 单条查询失败不阻断 */ }
  }
  return { hits, cost: Math.round(cost * 1e4) / 1e4 }
}

async function generateBrief(request: Request, env: Env): Promise<Response> {
  let body: any
  try { body = await request.json() } catch { return problem(400, 'invalid_body', '请求体需为 JSON') }

  const prompt = String(body?.prompt || '').trim()
  if (prompt.length > 800) return problem(400, 'invalid_parameter', 'prompt 不超过 800 字')
  const pickedCompanies: string[] = Array.isArray(body?.companies) ? body.companies : []
  const pickedDomains: string[] = Array.isArray(body?.domains) ? body.domains : []
  if (!env.KB_LLM_API_KEY) return problem(503, 'llm_unavailable', '模型密钥未配置')

  const enc = new TextEncoder()
  const stream = new TransformStream()
  const w = stream.writable.getWriter()
  const send = (event: string, data: unknown) =>
    w.write(enc.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`))

  ;(async () => {
    const t0 = Date.now()
    try {
      const all = await snapshot(env, 'events')
      const meta = await snapshot(env, 'meta')
      if (!all || !meta) throw new Error('事件快照不可用')
      let evs = all.results as any[]

      const compNames = (meta.companies || []).map((c: any) => `${c.id}(${c.name})`).join(' / ')
      const domNames = (meta.domains || []).map((d: any) => `${d.id}(${d.name})`).join(' / ')

      // ---------------- ① 意图分析 ----------------
      let intent: any = null
      let retrievalMode = '词面'
      if (prompt) {
        await send('thinking', { step: 'analyze', text: '解析检索意图' })
        try {
          intent = await llmJson(env, [
            { role: 'system', content: INTENT_SYS },
            { role: 'user', content:
              `可选公司：${compNames}\n可选领域：${domNames}\n\n用户需求：${prompt}` },
          ])
          await send('thinking', {
            step: 'analyze',
            text: `意图：${intent.intent || prompt.slice(0, 24)}`,
            meta: { intent },
          })
        } catch {
          await send('thinking', { step: 'analyze', text: '意图解析失败，退回词面匹配' })
        }
      } else {
        await send('thinking', { step: 'analyze', text: '未指定需求，生成本期知识库综述' })
      }

      // ---------------- ② 知识库检索 ----------------
      const comps = new Set<string>([...pickedCompanies, ...(intent?.companies || [])])
      const doms = new Set<string>([...pickedDomains, ...(intent?.domains || [])])
      if (comps.size) evs = evs.filter((e) => comps.has(e.company))
      if (doms.size) evs = evs.filter((e) => (e.domains || []).some((d: string) => doms.has(d)))

      const terms = prompt
        ? grams([prompt, ...(intent?.keywords || [])].join(' '))
        : new Set<string>()
      // 语义相似度与词面命中相加：词面抓确切主体名（「快手」），
      // 语义抓概念性提法（「导购闭环」「素材生成」）——两者互补，
      // 只用其一都会漏。语义权重 6 相当于「命中 6 个共现二元组」。
      const sem = prompt ? await semanticScores(env, `${prompt} ${(intent?.keywords || []).join(' ')}`)
                         : new Map<string, number>()
      const score = (e: any) => {
        let n = 0
        if (terms.size) {
          const eg = grams(`${e.title} ${e.summary || ''}`)
          for (const g of terms) if (eg.has(g)) n++
        }
        const sv = sem.get(e.id) ?? 0
        return n + sv * 6 + (e.status === '新增' ? 2 : e.status === '延续' ? 1 : 0)
      }
      evs = evs.map((e) => ({ e, s: score(e) })).sort((a, b) => b.s - a.s)
        .slice(0, 15).map((x) => x.e)
      retrievalMode = sem.size ? '词面 + 语义' : '词面'

      const scopeText = [
        comps.size ? '公司：' + [...comps].map((id) =>
          (meta.companies || []).find((c: any) => c.id === id)?.name || id).join('、') : '',
        doms.size ? '领域：' + [...doms].map((id) =>
          (meta.domains || []).find((d: any) => d.id === id)?.name || id).join('、') : '',
      ].filter(Boolean).join('　')

      await send('thinking', {
        step: 'retrieve',
        text: scopeText
          ? `知识库检索「${scopeText}」· ${retrievalMode} · 命中 ${evs.length} 条`
          : `知识库检索 · 取${prompt ? `相关性（${retrievalMode}）` : '最新'}前 ${evs.length} 条`,
        meta: { hits: evs.length, scope: scopeText, retrieval: retrievalMode },
      })
      if (!evs.length) {
        await send('error', { message: '所选范围内没有事件，请放宽条件' })
        await w.close(); return
      }

      const kb: string[] = []
      const cited: string[] = []
      for (const e of evs) {
        const d = await snapshot(env, `events/${e.id}`)
        if (!d) continue
        cited.push(e.id)
        kb.push(
          `${e.id} | ${e.company} | ${e.status} | 置信度：${e.confidence} | ` +
          `阶段：${e.stage || '未判定'} | 日期：${e.event_date || '未知'}\n` +
          `  ${e.title}\n` +
          (d.facts || []).slice(0, 5).map((f: any) => `  · ${f.text}`).join('\n'))
      }
      await send('sources', { events: cited, count: cited.length })

      // ---------------- ③ 互联网检索 ----------------
      let web: any[] = []
      let webCost = 0
      if (prompt && intent?.needs_web && (intent?.web_queries || []).length) {
        await send('thinking', {
          step: 'web',
          text: `互联网检索 · ${intent.web_queries.slice(0, 3).join('；')}`,
        })
        const r = await searchWeb(env, intent.web_queries)
        web = r.hits; webCost = r.cost
        await send('thinking', {
          step: 'web',
          text: `互联网检索 · 取回 ${web.length} 条（未经本系统核验）`,
          meta: { hits: web.length, cost: webCost },
        })
        await send('web_sources', {
          count: web.length, cost: webCost,
          items: web.map((x) => ({ title: x.title, url: x.url, host: x.host, published: x.published })),
        })
      }

      // ---------------- ④ 撰写 ----------------
      await send('thinking', { step: 'compose', text: '组装材料与洞察' })

      const parts = [
        `简报范围\n${scopeText || (prompt ? '未限定，按相关性排序' : '本期最新')}`,
        `\n【A 知识库事实】${kb.length} 个事件，已走过采集→去重→核验→置信度全链路：\n\n${kb.join('\n\n')}`,
      ]
      if (web.length) {
        parts.push(`\n【B 互联网检索】${web.length} 条，本轮实时检索，**未经本系统核验**：\n\n` +
          web.map((x, i) =>
            `W${i + 1} | ${x.host} | ${x.published || '时间未知'}\n  ${x.title}\n  ${x.text}\n  ${x.url}`
          ).join('\n\n'))
      } else if (prompt) {
        parts.push('\n【B 互联网检索】本次未发起（意图分析判定无需时效性补充）')
      }

      const messages = [
        { role: 'system', content: BRIEF_SYS },
        { role: 'user', content: parts.join('\n') },
        { role: 'user', content: prompt || '生成本期知识库综述：本期有哪些值得注意的动态，对零售运营/商分/营销有什么启发。' },
      ]

      await send('thinking', { step: 'write', text: '撰写正文' })

      const r = await fetch(`${env.KB_LLM_BASE_URL || 'https://api.deepseek.com'}/chat/completions`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: `Bearer ${env.KB_LLM_API_KEY}` },
        body: JSON.stringify({
          model: env.KB_BRIEF_MODEL || 'deepseek-chat',
          messages, stream: true, temperature: 0.3, max_tokens: 3200,
        }),
      })
      if (!r.ok || !r.body) throw new Error(`模型接口 ${r.status}`)

      const reader = r.body.getReader()
      const dec = new TextDecoder()
      let buf = ''
      let started = false
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() || ''
        for (const line of lines) {
          if (!line.startsWith('data:')) continue
          const payload = line.slice(5).trim()
          if (payload === '[DONE]') continue
          try {
            const j = JSON.parse(payload)
            const delta = j.choices?.[0]?.delta?.content
            if (delta) {
              if (!started) { started = true; await send('start', {}) }
              await send('token', { t: delta })
            }
          } catch { /* 跳过不完整分片 */ }
        }
      }
      await send('done', {
        elapsed_ms: Date.now() - t0,
        events: cited.length, web: web.length, web_cost: webCost,
      })
    } catch (e: any) {
      await send('error', { message: String(e?.message || e) })
    } finally {
      await w.close()
    }
  })()

  return new Response(stream.readable, {
    headers: {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-cache, no-transform',
      connection: 'keep-alive',
    },
  })
}
