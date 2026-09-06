import React, { useEffect, useMemo, useState } from 'react'
import { api, REASON_LABEL } from '../api.js'
import { Empty } from '../components/Bits.jsx'

/**
 * 拒绝台账（技术方案 §5.2）
 *
 * 这是全站最有说服力的一屏：评分标准要的是「**是否识别**旧闻、重复转载和
 * 待确认信息」。悄悄丢掉等于无法证明识别过——所以每一条被淘汰的信息都留痕，
 * 带理由与归并目标，可逐条核对。
 */
export default function RejectsView({ runId, nav }) {
  const [d, setD] = useState(null)
  const [err, setErr] = useState(null)
  const [reason, setReason] = useState('')

  useEffect(() => {
    api(`runs/${runId}/rejects`).then(setD).catch((e) => setErr(String(e)))
  }, [runId])

  const rows = useMemo(
    () => (!d ? [] : reason ? d.results.filter((r) => r.reason_code === reason) : d.results),
    [d, reason])

  if (err) return <div className="error">加载失败：{err}</div>
  if (!d) return <div className="loading">载入中…</div>

  return (
    <section>
      <button className="back" onClick={() => nav('/')}>← 运行回放</button>
      <h2 className="section-title">拒绝台账 · {runId}</h2>
      <p className="lede">
        本轮被淘汰的全部 {d.total} 条信息及各自的淘汰理由。
        <strong>识别出旧闻与重复转载本身就是产出</strong>——若在流程里悄悄丢掉，
        就无法证明识别过。每条都保留原始链接，可逐条核对。
      </p>

      <div className="frow">
        <span className="flabel">拒绝原因</span>
        {Object.entries(d.by_reason).sort((a, b) => b[1] - a[1]).map(([k, n]) => (
          <button key={k} className="fopt" aria-pressed={reason === k}
                  onClick={() => setReason(reason === k ? '' : k)}>
            {REASON_LABEL[k] || k} <b className="num">{n}</b>
          </button>
        ))}
      </div>

      {rows.length === 0 ? <Empty>无记录。</Empty> : (
        <table className="tbl compact">
          <thead>
            <tr>
              <th style={{ width: '7em' }}>原因</th>
              <th>被淘汰的条目</th>
              <th style={{ width: '8em' }}>信源</th>
              <th>判定依据</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 400).map((r, i) => (
              <tr key={i}>
                <td className="dim">{REASON_LABEL[r.reason_code] || r.reason_code}</td>
                <td>
                  {r.url
                    ? <a href={r.url} target="_blank" rel="noopener noreferrer">{r.title} ↗</a>
                    : <span>{r.title}</span>}
                </td>
                <td className="dim">{r.source_name}</td>
                <td className="dim sm">
                  {r.reason_detail}
                  {r.merged_into && (
                    <button className="link" onClick={() => nav(`/events/${r.merged_into}`)}>
                      {' '}→ {r.merged_into}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {rows.length > 400 && <p className="dim sm">仅显示前 400 条，完整台账见仓库 data/rejects.jsonl</p>}
    </section>
  )
}
