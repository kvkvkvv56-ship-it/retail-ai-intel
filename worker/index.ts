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
      return problem(501, 'not_implemented', '定制简报 SSE 尚未上线')
    }

    return problem(404, 'not_found', `未知接口 ${p}`)
  },
}
