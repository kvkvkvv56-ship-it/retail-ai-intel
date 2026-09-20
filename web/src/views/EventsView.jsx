import React, { useEffect, useMemo, useRef, useState } from 'react'
import { api, search, CONFIDENCE_ORDER } from '../api.js'
import { Confidence, Status, StageScale, ReviewPill, Empty, Loading, stagger } from '../components/Bits.jsx'

/**
 * 语义检索接线。
 *
 * 此前这个搜索框是纯客户端 substring：搜「导购闭环」只能命中标题里正好写着
 * 这四个字的事件，而事件向量、/api/v1/search 的词面+语义混排早就做好了，
 * 只是没人调。现在输入 ≥2 字就打后端，拿回的 id 顺序即相关性顺序。
 *
 * 三条降级，缺一不可：
 *   · 接口失败 / 离线 / 本地 dev 无 Worker  → 退回原来的 substring，行为不变
 *   · 后端缺 JINA_API_KEY                  → 它自己退回纯词面，mode 字段会说
 *   · 请求还在路上                          → 先用 substring 结果顶着，不留白屏
 *
 * 防抖 220ms，并在下一次输入时 abort 上一条——否则快速打字会让先发出的
 * 慢响应后到，把新结果盖掉。
 */
function useSemanticSearch(q) {
  const [hit, setHit] = useState(null)     // { ids: Map<id, sim>, mode, total }
  const abort = useRef(null)

  useEffect(() => {
    const kw = q.trim()
    abort.current?.abort()
    if (kw.length < 2) { setHit(null); return }
    const ac = new AbortController()
    abort.current = ac
    const t = setTimeout(async () => {
      const d = await search(kw, { signal: ac.signal })
      if (ac.signal.aborted) return
      setHit(d ? {
        ids: new Map(d.results.map((r, i) => [r.id, { rank: i, sim: r.similarity, by: r.matched }])),
        mode: d.mode, total: d.total,
      } : null)
    }, 220)
    return () => { clearTimeout(t); ac.abort() }
  }, [q])

  return hit
}

export default function EventsView({ meta, nav }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [f, setF] = useState({ company: new Set(), domain: new Set(), status: new Set(), confidence: new Set() })
  const [q, setQ] = useState('')
  const hit = useSemanticSearch(q)

  useEffect(() => { api('events').then(setData).catch((e) => setErr(String(e))) }, [])

  const rows = useMemo(() => {
    if (!data) return []
    const kw = q.trim().toLowerCase()
    const facet = (r) =>
      (!f.company.size || f.company.has(r.company)) &&
      (!f.domain.size || (r.domains || []).some((d) => f.domain.has(d))) &&
      (!f.status.size || f.status.has(r.status)) &&
      (!f.confidence.size || f.confidence.has(r.confidence))

    // 筛选条件始终在前端生效：后端检索不认识 company/domain 这些维度，
    // 把它的结果直接铺开会让已勾选的筛选看起来失灵
    if (hit) {
      return data.results.filter((r) => facet(r) && hit.ids.has(r.id))
        .map((r) => ({ ...r, _m: hit.ids.get(r.id) }))
        .sort((a, b) => a._m.rank - b._m.rank)
    }
    return data.results.filter((r) =>
      facet(r) && (!kw || (r.title + (r.summary || '')).toLowerCase().includes(kw)))
  }, [data, f, q, hit])

  if (err) return <div className="error">加载失败：{err}</div>
  if (!data) return <Loading />

  const compName = Object.fromEntries(meta.companies.map((c) => [c.id, c.name]))
  const domName = Object.fromEntries(meta.domains.map((d) => [d.id, d.name]))
  const active = Object.values(f).reduce((n, s) => n + s.size, 0)
  const toggle = (k) => (v) => setF((s) => {
    const next = new Set(s[k])
    next.has(v) ? next.delete(v) : next.add(v)
    return { ...s, [k]: next }
  })

  return (
    <div className="view">
      <div className="filterbar">
        <div className="search-wrap feed-search">
          <input placeholder="检索事件…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <div className="fgroups">
          <Chips label="公司" opts={meta.companies.map((c) => [c.id, c.name])}
                 cur={f.company} on={toggle('company')} />
          <Chips label="领域" opts={meta.domains.map((d) => [d.id, d.name])}
                 cur={f.domain} on={toggle('domain')} />
          <Chips label="状态" opts={meta.statuses.map((s) => [s, s === '旧闻/背景' ? '背景' : s])}
                 cur={f.status} on={toggle('status')} />
          <Chips label="置信度" opts={CONFIDENCE_ORDER.map((c) => [c, c])}
                 cur={f.confidence} on={toggle('confidence')} />
        </div>
        {active > 0 && (
          <button className="fclear" onClick={() => setF({ company: new Set(), domain: new Set(), status: new Set(), confidence: new Set() })}>
            清除筛选 {active}
          </button>
        )}
      </div>

      {/* 检索模式必须外显。后端缺 key 时会静默退回词面，用户搜到的东西
          会悄悄变差而页面毫无表示——这正是这个项目在别处反复拦过的那种
          静默降级。 */}
      {hit && (
        <div className="search-mode">
          {hit.mode === 'lexical+semantic' ? '词面 + 语义检索' : '词面检索（语义不可用）'}
          <span className="n">后端命中 {hit.total} · 当前筛选下 {rows.length}</span>
        </div>
      )}

      {rows.length === 0 ? <Empty>没有符合条件的事件</Empty> : rows.map((r, i) => (
        <article key={r.id} className="ev-card stagger" style={stagger(i)}
                 onClick={() => nav(`/events/${r.id}`)}>
          <div className="ev-rail">
            <span className="d1">{(r.event_date || '').slice(5) || '—'}</span>
            <span className="d2">{(r.event_date || '').slice(0, 4)}</span>
            <span className="bar" />
          </div>
          <div className="ev-body">
            <div className="ev-tags">
              <span className="pill co">{compName[r.company] || r.company}</span>
              <Status value={r.status} />
              <Confidence value={r.confidence} />
              <ReviewPill value={r.review_state} />
              {/* 语义命中要标出来：标题里没有这个词却排在前面，不说明理由
                  会让人以为搜错了 */}
              {r._m?.by === 'semantic' && (
                <span className="pill sem" title={`向量相似度 ${r._m.sim ?? '—'}`}>
                  语义 {r._m.sim != null ? r._m.sim.toFixed(2) : ''}
                </span>
              )}
            </div>
            <h3 className="ev-title">{r.title}</h3>
            {r.summary && <p className="ev-sum">{r.summary}</p>}
            <div className="ev-foot">
              <span>{(r.domains || []).map((d) => domName[d] || d).join(' · ') || '未分类'}</span>
              <StageScale stages={meta.stages} value={r.stage} />
              <span>{r.n_sources} 源 · {r.independent_orgs} 组织 · {r.n_facts} 事实</span>
            </div>
          </div>
        </article>
      ))}
    </div>
  )
}

const Chips = ({ label, opts, cur, on }) => (
  <div className="frow">
    <span className="flabel">{label}</span>
    {opts.map(([v, name]) => (
      <button key={v} className={`fchip ${cur.has(v) ? 'on' : ''}`} onClick={() => on(v)}>{name}</button>
    ))}
  </div>
)
