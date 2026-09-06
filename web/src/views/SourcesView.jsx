import React, { useEffect, useState } from 'react'
import { api, CLASS_LABEL, fmtDate } from '../api.js'

/**
 * 信源健康度（技术方案 §3.4）
 * 直接对应「信息源是否多元、可靠」的评分说明，也是汰换信源的依据。
 */
const ACTION_LABEL = {
  same: '判定为同一事件并合并', split: '判定为不同事件',
  confirm: '确认采信', reject: '驳回', watch: '转持续观察',
  retier: '信源分级调整', flag: '系统标记疑似重复',
}

export default function SourcesView() {
  const [d, setD] = useState(null)
  const [rev, setRev] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => {
    api('sources').then(setD).catch((e) => setErr(String(e)))
    api('reviews').then(setRev).catch(() => {})
  }, [])
  if (err) return <div className="error">加载失败：{err}</div>
  if (!d) return <div className="loading">载入中…</div>

  return (
    <div className="view">
      <p className="sec-title">信源体系 · {d.total} 个</p>
      <table className="tbl compact">
        <thead>
          <tr>
            <th>信源</th>
            <th style={{ width: '8em' }}>一手性</th>
            <th style={{ width: '5em' }}>通道</th>
            <th className="num" style={{ width: '4.5em' }}>命中</th>
            <th className="num" style={{ width: '4.5em' }}>采纳</th>
            <th className="num" style={{ width: '5em' }}>采纳率</th>
            <th>备注</th>
          </tr>
        </thead>
        <tbody>
          {d.results.map((s) => (
            <tr key={s.id} className={s.hits ? '' : 'faded'}>
              <td>
                {s.name}
                {s.affiliated_with && <span className="tag">自家媒体·不计独立源</span>}
              </td>
              <td className="d">{s.source_class} {CLASS_LABEL[s.source_class]}</td>
              <td className="d">{s.channel}</td>
              <td className="num">{s.hits}</td>
              <td className="num">{s.accepted}</td>
              <td className="num dim">
                {s.accept_rate == null ? '—' : `${Math.round(s.accept_rate * 100)}%`}
              </td>
              <td className="d">{s.note || ''}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {rev && rev.total > 0 && (
        <>
          <p className="sec-title mt">
            人工复核记录 · {rev.results.filter((r) => r.reviewer !== 'system').length} 次
          </p>
          <table className="tbl compact">
            <thead>
              <tr>
                <th style={{ width: '6.5em' }}>时间</th>
                <th style={{ width: '9em' }}>操作</th>
                <th style={{ width: '11em' }}>对象</th>
                <th>依据</th>
              </tr>
            </thead>
            <tbody>
              {rev.results.filter((r) => r.reviewer !== 'system').map((r, i) => (
                <tr key={i}>
                  <td className="num dim">{fmtDate(r.created_at)}</td>
                  <td>{ACTION_LABEL[r.action] || r.action}</td>
                  <td className="dim num sm">{r.target_id}</td>
                  <td className="d">{r.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}
