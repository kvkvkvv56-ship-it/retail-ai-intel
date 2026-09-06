import React, { useEffect, useMemo, useState } from 'react'
import { api, CONFIDENCE_ORDER } from '../api.js'
import { Confidence, Status, StageScale, Empty } from '../components/Bits.jsx'

export default function EventsView({ meta, nav }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [f, setF] = useState({ company: new Set(), domain: new Set(), status: new Set(), confidence: new Set() })
  const [q, setQ] = useState('')

  useEffect(() => { api('events').then(setData).catch((e) => setErr(String(e))) }, [])

  const rows = useMemo(() => {
    if (!data) return []
    const kw = q.trim().toLowerCase()
    return data.results.filter((r) =>
      (!f.company.size || f.company.has(r.company)) &&
      (!f.domain.size || (r.domains || []).some((d) => f.domain.has(d))) &&
      (!f.status.size || f.status.has(r.status)) &&
      (!f.confidence.size || f.confidence.has(r.confidence)) &&
      (!kw || (r.title + (r.summary || '')).toLowerCase().includes(kw)))
  }, [data, f, q])

  if (err) return <div className="error">加载失败：{err}</div>
  if (!data) return <div className="loading">载入中…</div>

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

      {rows.length === 0 ? <Empty>没有符合条件的事件</Empty> : rows.map((r) => (
        <article key={r.id} className="ev-card" onClick={() => nav(`/events/${r.id}`)}>
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
              {r.review_state && r.review_state !== 'none' && r.review_state !== 'confirmed' &&
                <span className="pill warn">待复核</span>}
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
