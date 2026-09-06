const cache = new Map()

export async function api(path) {
  if (cache.has(path)) return cache.get(path)
  const p = fetch(`/api/v1/${path}`).then(async (r) => {
    if (!r.ok) {
      const body = await r.json().catch(() => ({}))
      throw new Error(body.detail || `${r.status} ${path}`)
    }
    return r.json()
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
