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
  KB_LLM_API_KEY?: string
  KB_LLM_BASE_URL?: string
  KB_BRIEF_MODEL?: string
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

async function snapshot(env: Env, path: string): Promise<any | null> {
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
        || route === 'runs' || route === 'reports') {
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
    const detail = route.match(/^(events|runs|reports)\/([A-Za-z0-9\-]+)$/)
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
      const hits = idx.filter(
        (e) => e.t.toLowerCase().includes(lower) || (e.s || '').toLowerCase().includes(lower),
      )
      return ok({ query: q, total: hits.length, results: hits.slice(0, 50) })
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

const BRIEF_SYS = `你是「行业与竞对 AI 洞察助手」的简报撰写智能体，基于知识库上下文生成定制简报。

输出格式：
- Markdown：## 二级标题分节、要点列表、关键数字加粗
- 数据对比优先用 Markdown 表格
- 长度克制：信息密度优先，不注水、不复述上下文原文

内容边界：
- 只使用「知识库上下文」中的事件与数据，不编造知识库外的事实
- 事实（来自事件）与推断（你的分析）分开表述，推断用「判断」「推测」明确标注
- 引用事件时写出 EV 编号，便于读者核对
- 知识库无法覆盖的部分，直接说明信息缺口，不强行补全
- 涉及借鉴建议时，标注适用受众（零售运营/商分/营销）与是否需要内部数据验证

语气：专业、克制、直接。`

/**
 * 消息三层组装（技术方案 §9.2）
 *   system : 固定行为约束，永不被变量污染
 *   user 前段 : 服务端渲染的简报范围与知识库上下文
 *   user 末段 : 用户原始提示词，永远放最后 —— 离指令出口最近，遵从权重最高
 */
async function generateBrief(request: Request, env: Env): Promise<Response> {
  let body: any
  try {
    body = await request.json()
  } catch {
    return problem(400, 'invalid_body', '请求体需为 JSON')
  }

  const prompt = String(body?.prompt || '').trim()
  if (prompt.length < 2 || prompt.length > 800) {
    return problem(400, 'invalid_parameter', 'prompt 长度需在 2-800 之间')
  }
  const companies: string[] = Array.isArray(body?.companies) ? body.companies : []
  const domains: string[] = Array.isArray(body?.domains) ? body.domains : []

  if (!env.KB_LLM_API_KEY) {
    return problem(503, 'llm_unavailable', '模型密钥未配置，简报功能暂不可用')
  }

  const enc = new TextEncoder()
  const stream = new TransformStream()
  const w = stream.writable.getWriter()
  const send = (event: string, data: unknown) =>
    w.write(enc.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`))

  ;(async () => {
    const t0 = Date.now()
    try {
      await send('thinking', { step: 'analyze', text: '分析简报范围与筛选条件' })

      const all = await snapshot(env, 'events')
      if (!all) throw new Error('事件快照不可用')
      let evs = all.results as any[]
      if (companies.length) evs = evs.filter((e) => companies.includes(e.company))
      if (domains.length) evs = evs.filter((e) => (e.domains || []).some((d: string) => domains.includes(d)))
      evs = evs.filter((e) => e.status === '新增' || e.status === '延续').slice(0, 18)

      await send('thinking', {
        step: 'retrieve',
        text: `检索知识库事件 · 命中 ${evs.length} 条`,
        meta: { hits: evs.length },
      })

      if (evs.length === 0) {
        await send('error', { message: '所选范围内没有本期事件，请放宽筛选条件' })
        await w.close()
        return
      }

      await send('thinking', { step: 'facts', text: '提取事实要点与信源' })
      const ctx: string[] = []
      const cited: string[] = []
      for (const e of evs) {
        const d = await snapshot(env, `events/${e.id}`)
        if (!d) continue
        cited.push(e.id)
        const facts = (d.facts || []).slice(0, 5).map((f: any) => `  · ${f.text}`).join('\n')
        ctx.push(
          `${e.id} | ${e.company} | ${e.status} | ${e.confidence} | 阶段：${e.stage || '未判定'} | 日期：${e.event_date || '未知'}\n` +
          `  标题：${e.title}\n${facts}`,
        )
      }

      await send('sources', { events: cited, count: cited.length })
      await send('thinking', { step: 'compose', text: '生成洞察并组装内容' })

      const scope = [
        companies.length ? `公司：${companies.join('、')}` : '公司：全部',
        domains.length ? `领域：${domains.join('、')}` : '领域：全部',
      ].join('　')

      const messages = [
        { role: 'system', content: BRIEF_SYS },
        { role: 'user', content: `简报范围\n${scope}\n\n知识库上下文（${ctx.length} 个事件）：\n\n${ctx.join('\n\n')}` },
        { role: 'user', content: prompt },
      ]

      const r = await fetch(`${env.KB_LLM_BASE_URL || 'https://api.deepseek.com'}/chat/completions`, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          authorization: `Bearer ${env.KB_LLM_API_KEY}`,
        },
        body: JSON.stringify({
          model: env.KB_BRIEF_MODEL || 'deepseek-chat',
          messages,
          stream: true,
          temperature: 0.3,
          max_tokens: 2600,
        }),
      })
      if (!r.ok || !r.body) throw new Error(`模型接口 ${r.status}`)

      await send('thinking', { step: 'write', text: '撰写正文' })

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
      await send('done', { elapsed_ms: Date.now() - t0, events: cited.length })
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
