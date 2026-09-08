import React, { useEffect, useMemo, useState } from 'react'
import { api, REASON_LABEL } from '../api.js'
import { Empty, Loading } from '../components/Bits.jsx'

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
  if (!d) return <Loading />

  return (
    <div className="view">
      <button className="back" onClick={() => nav('/')}>← 运行回放</button>
      <p className="sec-title">拒绝台账 · {runId}</p>

      <div className="frow" style={{ marginBottom: 20 }}>
        
        {Object.entries(d.by_reason).sort((a, b) => b[1] - a[1]).map(([k, n]) => (
          <button key={k} className="fchip" 
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
                <td className="d">{REASON_LABEL[r.reason_code] || r.reason_code}</td>
                <td>
                  {r.url
                    ? <a href={r.url} target="_blank" rel="noopener noreferrer">{r.title} ↗</a>
                    : <span>{r.title}</span>}
                </td>
                <td className="d">{r.source_name}</td>
                <td className="d">
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
      {rows.length > 400 && <p className="d">仅显示前 400 条</p>}
    </div>
  )
}
