import React, { useEffect, useState } from 'react'
import { api, fmtDate } from '../api.js'
import { Empty } from '../components/Bits.jsx'

export default function ReportView({ meta, nav }) {
  const [list, setList] = useState(null)
  const [rep, setRep] = useState(null)
  const [err, setErr] = useState(null)

  useEffect(() => {
    api('reports')
      .then((d) => {
        setList(d)
        return d.results.length ? api(`reports/${d.results[0].id}`) : null
      })
      .then(setRep)
      .catch((e) => setErr(String(e)))
  }, [])

  if (err) return <div className="error">加载失败：{err}</div>
  if (!list) return <div className="loading">载入中…</div>
  if (!rep) return <Empty>尚无周报</Empty>

  const b = rep.body || {}
  const domName = Object.fromEntries(meta.domains.map((d) => [d.id, d.name]))

  return (
    <div className="view">
      {/* 历史存档：往期一行排开，当前期高亮。周报是持续产出物，
          「能翻到上几期」本身就是可持续运行的证据 */}
      {list.total > 1 && (
        <div className="archive">
          <span className="arch-k">往期</span>
          {list.results.map((r) => (
            <button key={r.id}
                    className={`arch-item ${r.id === rep.id ? 'on' : ''}`}
                    onClick={() => api(`reports/${r.id}`).then(setRep)}>
              <span className="num">{r.period_start.slice(5)}</span>
              <span className="arch-hl">{r.headline}</span>
            </button>
          ))}
        </div>
      )}

      <p className="sec-title">
        {rep.period_start} ~ {rep.period_end}
        <span className="d"> · 第 {list.results.length - list.results.findIndex((r) => r.id === rep.id)} 期 / 共 {list.total} 期</span>
      </p>

      <div className="headline-banner">
        <div className="k">本期主线</div>
        <h3>{b.headline}</h3>
      </div>

      {/* -------- 关键发现：每条锚定证据事件，可点进去核对 -------- */}
      <h3 className="sub-title">
        关键发现 <span className="n">{(b.key_findings || []).length}</span>
      </h3>
      {(b.key_findings || []).map((f, i) => (
        <article className="finding" key={i}>
          <span className="no">{String(i + 1).padStart(2, '0')}</span>
          <h4>{f.title}</h4>
          <p>{f.detail}</p>
          <div className="evi">
            {(f.evidence || []).map((id) => (
              <button key={id} className="ev" onClick={() => nav(`/events/${id}`)}>{id}</button>
            ))}
          </div>
        </article>
      ))}

      {/* -------- 延续性检查：上期观察项是否兑现 -------- */}
      {(b.continuity || []).length > 0 && (
        <>
          <h3 className="sub-title">延续性检查</h3>
          <table className="tbl compact">
            <thead>
              <tr><th>上期观察项</th><th style={{ width: '6em' }}>本期</th><th>依据</th></tr>
            </thead>
            <tbody>
              {b.continuity.map((c, i) => (
                <tr key={i}>
                  <td>{c.prev_watch}</td>
                  <td className="dim nowrap">{c.status}</td>
                  <td className="d">{c.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {/* -------- 分领域态势 -------- */}
      <h3 className="sub-title">分领域态势</h3>
      <div className="domains">
        {Object.entries(b.domain_summaries || {}).map(([k, v]) => (
          <section className="domcard" key={k}>
            <h4>{domName[k] || k}</h4>
            <p>{v.summary}</p>
            {v.watch && <p className="watch">下期观察：{v.watch}</p>}
          </section>
        ))}
      </div>

      {/* -------- 借鉴建议：本助手产出，无信源 -------- */}
      {(b.implications || []).length > 0 && (
        <>
          <h3 className="sub-title">
            借鉴建议 <span className="n">助手产出</span>
          </h3>
          <ul className="claims">
            {b.implications.map((im, i) => (
              <li className="claim rec" key={i}>
                <p>{im.text}</p>
                <span className="attr">
                  适用：{im.audience || '未标注'}
                  {im.needs_internal_data && <b> · 需内部数据验证</b>}
                  {(im.based_on || []).map((id) => (
                    <button key={id} className="ev" onClick={() => nav(`/events/${id}`)}>
                      {id}
                    </button>
                  ))}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      {/* -------- 观察清单 -------- */}
      {(b.watchlist || []).length > 0 && (
        <>
          <h3 className="sub-title">下期观察清单</h3>
          <ul className="wl">
            {b.watchlist.map((w, i) => (
              <li key={i}>
                {w.text}
                <span className="evi">
                  {(w.evidence || []).map((id) => (
                    <button key={id} className="ev" onClick={() => nav(`/events/${id}`)}>
                      {id}
                    </button>
                  ))}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      {/* -------- 方法论备注：可信度边界，主动暴露不确定性 -------- */}
      {b.method_notes && (
        <>
          <h3 className="sub-title">方法论备注 <span className="n">可信度边界</span></h3>
          <p className="method">{b.method_notes}</p>
        </>
      )}

      <p className="dim sm mt">
        生成于 {fmtDate(rep.generated_at)} · 运行 {rep.run_id}
      </p>
    </div>
  )
}
