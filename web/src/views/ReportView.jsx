import React, { useEffect, useState } from 'react'
import { api, fmtDate } from '../api.js'
import { Collapse, Empty, Loading, stagger } from '../components/Bits.jsx'

/** 侧栏只列最近这么多期。周报是持续产出物、期数单调增长，
 *  列表必须有个不随时间膨胀的上界；其余全部进「查看全部周报」页。 */
const SIDE_MAX = 5

export default function ReportView({ meta, nav, id }) {
  const [list, setList] = useState(null)
  const [rep, setRep] = useState(null)
  const [err, setErr] = useState(null)

  useEffect(() => {
    setRep(null); setErr(null)
    api('reports')
      .then((d) => {
        setList(d)
        const target = id || d.results[0]?.id
        return target ? api(`reports/${target}`) : null
      })
      .then(setRep)
      .catch((e) => setErr(String(e)))
  }, [id])

  if (err) return <div className="error">加载失败：{err}</div>
  if (!list) return <Loading />
  if (!rep) return <Empty>尚无周报</Empty>

  const b = rep.body || {}
  const domName = Object.fromEntries(meta.domains.map((d) => [d.id, d.name]))
  const idx = list.results.findIndex((r) => r.id === rep.id)

  return (
    <div className="view rep-layout">
      {/* 往期列表常驻左侧。原来横排在正文之上，期数一多就会把正文顶下去，
          而且没有可扩展的落点 */}
      {/* 窄屏：折叠成一行「第 N 期 · 日期」，点开选期。
          原来是横向滚动条，看不全也不知道自己在第几期 */}
      <Collapse className="rep-collapse" label="往期周报"
                current={`第 ${list.total - idx} 期 · ${rep.period_start.slice(5)}`}>
        {list.results.map((r, i) => (
          <button key={r.id} className={`cb-item ${r.id === rep.id ? 'on' : ''}`}
                  onClick={() => nav(`/report/${r.id}`)}>
            <span className="cb-t">第 {list.total - i} 期 · {r.period_start.slice(5)}</span>
            <span className="cb-d">{r.headline}</span>
          </button>
        ))}
      </Collapse>

      <aside className="rep-side">
        <p className="side-k">往期周报</p>
        <ul className="side-list">
          {list.results.slice(0, SIDE_MAX).map((r, i) => (
            <li key={r.id}>
              <button className={`side-item ${r.id === rep.id ? 'on' : ''}`}
                      onClick={() => nav(`/report/${r.id}`)}>
                <span className="side-d tnum">
                  第 {list.total - i} 期 · {r.period_start.slice(5).replace('-', '/')}
                </span>
                <span className="side-hl">{r.headline}</span>
              </button>
            </li>
          ))}
        </ul>
        <button className="side-more" onClick={() => nav('/reports')}>
          查看全部周报
          <span className="side-rest">共 {list.total} 期</span>
        </button>
      </aside>

      <div className="rep-main">
      <p className="sec-title">
        {rep.period_start} ~ {rep.period_end}
        <span className="d"> · 第 {list.total - idx} 期 / 共 {list.total} 期</span>
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
        <article className="finding stagger" style={stagger(i)} key={i}>
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
    </div>
  )
}
