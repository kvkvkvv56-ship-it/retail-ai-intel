import React, { useEffect, useState } from 'react'
import { api, CLASS_LABEL, fmtDate } from '../api.js'
import { Confidence, Status, TimeMark, Empty } from '../components/Bits.jsx'

export default function EventDetail({ id, meta, nav }) {
  const [e, setE] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => {
    setE(null); setErr(null)
    api(`events/${id}`).then(setE).catch((x) => setErr(String(x)))
  }, [id])

  if (err) return <div className="error">加载失败：{err}</div>
  if (!e) return <div className="loading">载入中…</div>

  const domName = Object.fromEntries(meta.domains.map((d) => [d.id, d.name]))
  const compName = Object.fromEntries(meta.companies.map((c) => [c.id, c.name]))
  const byId = Object.fromEntries((e.items || []).map((i) => [i.id, i]))

  return (
    <div className="view">
      <button className="back" onClick={() => nav('/events')}>← 事件库</button>

      <h2 className="detail-title">{e.title}</h2>
      <div className="meta-row">
        <span className="pill co">{compName[e.company] || e.company}</span>
        {(e.domains || []).map((d) => <span key={d} className="pill dom">{domName[d] || d}</span>)}
        <Status value={e.status} />
        <Confidence value={e.confidence} />
        <span className="num">{fmtDate(e.event_date)}</span>
        <span>{e.independent_orgs} 个独立组织</span>
      </div>
      {e.summary && <p className="ev-sum" style={{ margin: "16px 0", maxWidth: "68ch" }}>{e.summary}</p>}

      {e.stage && (
        <div className="callout">
          <span className="k">落地阶段</span>
          <span className="v">{e.stage}</span>
          <span className="note">{e.stage_basis}</span>
        </div>
      )}
      {e.review_state && e.review_state !== 'none' && (
        <div className="callout warn">
          <span className="k">人工复核</span>
          <span className="v">
            {e.review_state === 'pending' ? '单源待确认，已进复核队列'
              : '疑似与其他事件重复，待人工判定'}
          </span>
        </div>
      )}

      <h3 className="sub-title">事实 <span className="n">{e.facts.length}</span></h3>
      {e.facts.length === 0 ? <Empty>—</Empty> : (
        <ul className="claims">
          {e.facts.map((c) => {
            const it = byId[c.source_item_id]
            return (
              <li key={c.id} className="claim fact">
                <p>{c.text}</p>
                {it && (
                  <a className="src" href={it.url} target="_blank" rel="noopener noreferrer">
                    {it.source_name}
                    <span className="cls">{CLASS_LABEL[it.effective_class] || ''}</span>
                    <TimeMark at={it.published_at} source={it.time_source} /> ↗
                  </a>
                )}
              </li>
            )
          })}
        </ul>
      )}

      <h3 className="sub-title">推断 <span className="n">{e.inferences.length}</span></h3>
      {e.inferences.length === 0 ? <Empty>—</Empty> : (
        <ul className="claims">
          {e.inferences.map((c) => (
            <li key={c.id} className="claim infer">
              <p>{c.text}</p>
              <span className="attr">— {c.attributed_to || '报道方'} 的判断</span>
            </li>
          ))}
        </ul>
      )}

      {e.recommendations?.length > 0 && (
        <>
          <h3 className="sub-title">
            借鉴建议 <span className="n">助手产出</span>
          </h3>
          <ul className="claims">
            {e.recommendations.map((c) => (
              <li key={c.id} className="claim rec">
                <p>{c.text}</p>
                <span className="attr">
                  适用：{c.audience || '未标注'}
                  {c.needs_internal_data && <b> · 需内部数据验证</b>}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      <h3 className="sub-title">
        原始信源 <span className="n">{e.items.length}</span>
      </h3>
      <table className="tbl compact">
        <thead>
          <tr>
            <th>标题</th>
            <th style={{ width: '8em' }}>信源</th>
            <th style={{ width: '7em' }}>一手性</th>
            <th style={{ width: '6.5em' }}>发布</th>
            <th style={{ width: '6.5em' }}>采集</th>
            <th style={{ width: '4em' }}>通道</th>
          </tr>
        </thead>
        <tbody>
          {e.items.map((i) => (
            <tr key={i.id}>
              <td>
                <a href={i.url} target="_blank" rel="noopener noreferrer">{i.title} ↗</a>
                {i.original_source && (
                  <div className="d">原始出处归因 → {i.original_source}</div>
                )}
                {i.partial_content && <span className="tag">付费墙·仅摘要</span>}
              </td>
              <td className="d">{i.source_name}</td>
              <td className="d">
                {CLASS_LABEL[i.effective_class] || '—'}
                {i.effective_class !== i.source_class && (
                  <span className="tag">归因升级</span>
                )}
              </td>
              <td className="num dim"><TimeMark at={i.published_at} source={i.time_source} /></td>
              <td className="num dim">{fmtDate(i.discovered_at)}</td>
              <td className="d">{i.channel}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {e.edges?.length > 0 && (
        <>
          <h3 className="sub-title">关联事件</h3>
          <ul className="edges">
            {e.edges.map((g, i) => {
              const other = g.from_event === e.id ? g.to_event : g.from_event
              return (
                <li key={i}>
                  <button className="link" onClick={() => nav(`/events/${other}`)}>
                    {g.relation === 'follows' ? '后续进展' :
                      g.relation === 'same_actor_track' ? '同主体动作' : g.relation}
                    {' '}→ {other}
                  </button>
                  <span className="d"> {g.basis}</span>
                </li>
              )
            })}
          </ul>
        </>
      )}
    </div>
  )
}
