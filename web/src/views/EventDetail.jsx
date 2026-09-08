import React, { useEffect, useState } from 'react'
import { api, CLASS_LABEL, CLASS_SHORT, fmtDate } from '../api.js'
import { Confidence, Status, TimeMark, Empty, Loading } from '../components/Bits.jsx'

const REL_LABEL = {
  follows: '后续进展', same_actor_track: '同主体动作',
  corroborates: '相互印证', contradicts: '存在矛盾', causes: '因果关联',
}

export default function EventDetail({ id, meta, nav }) {
  const [e, setE] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => {
    setE(null); setErr(null)
    api(`events/${id}`).then(setE).catch((x) => setErr(String(x)))
  }, [id])

  if (err) return <div className="error">加载失败：{err}</div>
  if (!e) return <Loading />

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
                  <a className="src" href={it.url} target="_blank" rel="noopener noreferrer"
                     title={CLASS_LABEL[it.effective_class] || ''}>
                    <span className={`cls c${it.effective_class}`}>{it.effective_class}</span>
                    {it.source_name}
                    <span className="sep">·</span>
                    <TimeMark at={it.published_at} source={it.time_source} />
                    <span className="sep">↗</span>
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
      <table className="tbl src-tbl">
        <colgroup>
          <col /><col style={{ width: '9em' }} /><col style={{ width: '6.5em' }} />
          <col style={{ width: '6.5em' }} /><col style={{ width: '6.5em' }} />
          <col style={{ width: '4.5em' }} />
        </colgroup>
        <thead>
          <tr>
            <th>标题</th><th>信源</th><th>一手性</th>
            <th>发布</th><th>采集</th><th>通道</th>
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
              <td className="d nowrap" title={i.source_name}>{i.source_name}</td>
              <td className="nowrap" title={CLASS_LABEL[i.effective_class] || ''}>
                <span className={`cls c${i.effective_class}`}>{i.effective_class}</span>
                <span className="d"> {CLASS_SHORT[i.effective_class] || '—'}</span>
                {i.effective_class !== i.source_class && (
                  <span className="pill mini">归因升级</span>
                )}
              </td>
              <td className="tnum d nowrap"><TimeMark at={i.published_at} source={i.time_source} /></td>
              <td className="tnum d nowrap">{fmtDate(i.discovered_at)}</td>
              <td className="nowrap"><span className="chan">{i.channel}</span></td>
            </tr>
          ))}
        </tbody>
      </table>

      {e.edges?.length > 0 && (
        <>
          <h3 className="sub-title">关联事件</h3>
          <ul className="edges">
            {e.edges.map((g, i) => (
              <li key={i}>
                <button className="edge-item" onClick={() => nav(`/events/${g.other}`)}>
                  <span className="edge-rel">{REL_LABEL[g.relation] || g.relation}</span>
                  <span className="edge-title">{g.title || g.other}</span>
                  <span className="edge-meta num">
                    {g.event_date || '—'}{g.created_by === 'rule' ? ' · 规则' : ' · 模型'}
                  </span>
                </button>
                {/* 规则边的依据是模板化的「同为 X 的动作，时间相邻」，没有信息量，
                    不展示；模型提议的边带具体依据，值得展示 */}
                {g.relation !== 'same_actor_track' && g.basis && (
                  <div className="edge-basis">{g.basis}</div>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}
