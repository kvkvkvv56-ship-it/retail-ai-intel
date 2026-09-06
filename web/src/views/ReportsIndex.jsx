import React, { useEffect, useState } from 'react'
import { api, fmtDate } from '../api.js'
import { Empty } from '../components/Bits.jsx'

/**
 * 全部周报存档。侧栏只留最近 10 期，其余从这里进。
 * 每行给「期数 + 窗口 + 主线判断」——只给日期的话，读者无从判断值不值得点。
 */
export default function ReportsIndex({ nav }) {
  const [list, setList] = useState(null)
  const [err, setErr] = useState(null)

  useEffect(() => {
    api('reports').then(setList).catch((e) => setErr(String(e)))
  }, [])

  if (err) return <div className="error">加载失败：{err}</div>
  if (!list) return <div className="loading">载入中…</div>
  if (!list.results.length) return <Empty>尚无周报</Empty>

  return (
    <div className="view">
      <button className="back" onClick={() => nav('/report')}>← 周报</button>
      <p className="sec-title">
        全部周报<span className="d"> · 共 {list.total} 期</span>
      </p>

      <ul className="rep-index">
        {list.results.map((r, i) => (
          <li key={r.id}>
            <button className="rep-row" onClick={() => nav(`/report/${r.id}`)}>
              <span className="rep-no tnum">第 {list.total - i} 期</span>
              <span className="rep-win tnum d">{r.period_start} ~ {r.period_end}</span>
              <span className="rep-hl">{r.headline}</span>
              <span className="rep-gen tnum d">{fmtDate(r.generated_at)}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
