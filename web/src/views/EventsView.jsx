import React, { useEffect, useMemo, useState } from 'react'
import { api, CONFIDENCE_ORDER, fmtDate } from '../api.js'
import { Confidence, Status, Empty } from '../components/Bits.jsx'

export default function EventsView({ meta, nav }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [f, setF] = useState({ company: '', domain: '', status: '', confidence: '' })
  const [q, setQ] = useState('')

  useEffect(() => { api('events').then(setData).catch((e) => setErr(String(e))) }, [])

  const rows = useMemo(() => {
    if (!data) return []
    const kw = q.trim().toLowerCase()
    return data.results.filter((r) =>
      (!f.company || r.company === f.company) &&
      (!f.domain || (r.domains || []).includes(f.domain)) &&
      (!f.status || r.status === f.status) &&
      (!f.confidence || r.confidence === f.confidence) &&
      (!kw || r.title.toLowerCase().includes(kw) ||
        (r.summary || '').toLowerCase().includes(kw)))
  }, [data, f, q])

  if (err) return <div className="error">加载失败：{err}</div>
  if (!data) return <div className="loading">载入中…</div>

  const compName = Object.fromEntries(meta.companies.map((c) => [c.id, c.name]))
  const domName = Object.fromEntries(meta.domains.map((d) => [d.id, d.name]))
  const set = (k) => (v) => setF((s) => ({ ...s, [k]: s[k] === v ? '' : v }))

  return (
    <section>
      <h2 className="section-title">事件库 · {rows.length} / {data.total}</h2>
      <p className="lede">
        每个事件都可下钻到支撑它的事实、每条事实背后的原始信源，直至原文链接。
        事实、推断、建议三者在数据结构上强制分离——建议不允许有信源，
        能追溯到某篇文章的就不是建议，是别人的观点。
      </p>

      <div className="filters">
        <input className="search" placeholder="检索标题与概述…"
               value={q} onChange={(e) => setQ(e.target.value)} />
        <FilterRow label="公司" opts={meta.companies.map((c) => [c.id, c.name])}
                   cur={f.company} on={set('company')} />
        <FilterRow label="领域" opts={meta.domains.map((d) => [d.id, d.name])}
                   cur={f.domain} on={set('domain')} />
        <FilterRow label="状态" opts={meta.statuses.map((s) => [s, s])}
                   cur={f.status} on={set('status')} />
        <FilterRow label="置信度" opts={CONFIDENCE_ORDER.map((c) => [c, c])}
                   cur={f.confidence} on={set('confidence')} />
      </div>

      {rows.length === 0 ? <Empty>没有符合条件的事件。</Empty> : (
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ width: '7.5em' }}>事件日期</th>
              <th>事件</th>
              <th style={{ width: '7em' }}>公司</th>
              <th style={{ width: '5em' }}>阶段</th>
              <th style={{ width: '9em' }}>置信度</th>
              <th style={{ width: '5em' }}>状态</th>
              <th className="num" style={{ width: '7.5em' }}>信源/事实</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} onClick={() => nav(`/events/${r.id}`)} className="clickable">
                <td className="num dim">{fmtDate(r.event_date)}</td>
                <td>
                  <span className="ttl">{r.title}</span>
                  {r.review_state && r.review_state !== 'none' &&
                    <span className="tag">待复核</span>}
                  <div className="dim sm">
                    {(r.domains || []).map((d) => domName[d] || d).join(' · ') || '未分类'}
                  </div>
                </td>
                <td className="dim nowrap">{compName[r.company] || r.company}</td>
                <td className="dim">{r.stage || '—'}</td>
                <td><Confidence value={r.confidence} /></td>
                <td><Status value={r.status} /></td>
                <td className="num dim">{r.n_sources} / {r.n_facts}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

function FilterRow({ label, opts, cur, on }) {
  return (
    <div className="frow">
      <span className="flabel">{label}</span>
      {opts.map(([v, name]) => (
        <button key={v} className="fopt" aria-pressed={cur === v} onClick={() => on(v)}>
          {name}
        </button>
      ))}
    </div>
  )
}
