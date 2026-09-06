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

/** 置信度五级的展示序（技术方案 §6.1），由高到低 */
export const CONFIDENCE_ORDER = [
  '官方确认·多源印证', '官方一手', '多源已验证', '深度单源', '单源待确认',
]

/** 一手性五类（§3.1）：离事件现场有多近，与发布主体身份无关 */
export const CLASS_LABEL = {
  A: '当事方直述', B: '原创深度报道', C: '常规媒体报道',
  D: '聚合转载', E: '观点评论',
}

export const REASON_LABEL = {
  duplicate_url: 'URL 重复', syndication: '转载', stale: '超出时间窗口',
  off_topic: '与观察范围无关', irrelevant: 'LLM 判定不相关',
  merged: '归并入事件', no_title: '缺标题或链接',
}

export const fmtDate = (s) => (s || '').slice(0, 10) || '—'
