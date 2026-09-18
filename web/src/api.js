import fallback from './data/fallback.json'

const cache = new Map()

/** 离线（降级）状态。任一请求走了内嵌快照即置位，由 App 显示提示条。 */
export const offline = { on: false }
const listeners = new Set()
export const onOffline = (fn) => { listeners.add(fn); return () => listeners.delete(fn) }
function markOffline() {
  if (offline.on) return
  offline.on = true
  listeners.forEach((f) => f())
}

/**
 * 内嵌快照兜底（技术方案 §10.6）
 * 只覆盖首页与列表所需的最小集；详情页仍需在线。
 */
function fromFallback(path) {
  if (path === 'meta') return fallback.meta
  if (path === 'events') return fallback.events
  if (path === 'runs') return fallback.runs
  if (path === 'reports') return fallback.reports
  if (path.startsWith('runs/') && fallback.latest_run
      && path === `runs/${fallback.latest_run.id}`) return fallback.latest_run
  if (path.startsWith('reports/') && fallback.latest_report
      && path === `reports/${fallback.latest_report.id}`) return fallback.latest_report
  return null
}

export async function api(path) {
  if (cache.has(path)) return cache.get(path)
  const p = fetch(`/api/v1/${path}`)
    .then(async (r) => {
      if (!r.ok) {
        const body = await r.json().catch(() => ({}))
        throw new Error(body.detail || `${r.status} ${path}`)
      }
      return r.json()
    })
    .catch((e) => {
      const fb = fromFallback(path)
      if (fb) { markOffline(); return fb }
      throw e
    })
  cache.set(path, p)
  return p
}

/**
 * 语义检索（词面 + 向量）。
 *
 * 走 Worker 的 /api/v1/search：事件向量在导出时已经算好写进 vectors.json，
 * 线上只对用户这一句 query 调一次 embedding 接口，余弦在 Worker 里算。
 * 返回的 mode 字段说明本次实际用到了什么（lexical / lexical+semantic）——
 * 缺 JINA_API_KEY 时后端会静默退回纯词面，不显示出来的话用户无从知道
 * 自己搜到的到底是不是语义结果。
 *
 * 不走 api() 的那张 Map 缓存：query 千变万化，缓存只会无上限地涨。
 * 这里用一个带上限的小缓存，够覆盖「删掉一个字再加回来」这种即时往返。
 *
 * 失败一律返回 null，调用方退回本地词面匹配——检索框永远不该因为
 * 接口挂了就不能用。
 */
const searchCache = new Map()
const SEARCH_CACHE_MAX = 30

export async function search(q, { signal } = {}) {
  const key = q.trim().toLowerCase()
  if (key.length < 2 || key.length > 200) return null
  if (searchCache.has(key)) return searchCache.get(key)
  try {
    const r = await fetch(`/api/v1/search?q=${encodeURIComponent(key)}`, { signal })
    if (!r.ok) return null
    const d = await r.json()
    if (searchCache.size >= SEARCH_CACHE_MAX) {
      searchCache.delete(searchCache.keys().next().value)
    }
    searchCache.set(key, d)
    return d
  } catch {
    return null              // 含 AbortError：请求被下一次输入取代，静默即可
  }
}

/** 置信度五级的展示序（技术方案 §6.1），由高到低 */
export const CONFIDENCE_ORDER = [
  '官方确认·多源印证', '官方一手', '多源已验证', '深度单源', '单源待确认',
]

/** 一手性五类（§3.1）：离事件现场有多近，与发布主体身份无关 */
export const CLASS_LABEL = {
  A: '当事方直述', B: '原创深度报道', C: '常规媒体报道',
  D: '聚合转载', E: '观点评论',
}

/** 表格里用的短名——完整名在窄列里会折成两行，把行高撑起来 */
export const CLASS_SHORT = {
  A: '当事方', B: '深度原创', C: '常规报道', D: '聚合转载', E: '观点',
}

export const REASON_LABEL = {
  duplicate_url: 'URL 重复', syndication: '转载', stale: '超出时间窗口',
  off_topic: '与观察范围无关', irrelevant: 'LLM 判定不相关',
  merged: '归并入事件', no_title: '缺标题或链接',
}

export const fmtDate = (s) => (s || '').slice(0, 10) || '—'
