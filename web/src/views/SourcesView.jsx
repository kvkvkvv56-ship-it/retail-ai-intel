import React, { useEffect, useState } from 'react'
import { api, CLASS_LABEL, CLASS_SHORT, fmtDate } from '../api.js'

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
      <table className="tbl src-tbl">
        <colgroup>
          <col /><col style={{ width: '6.5em' }} /><col style={{ width: '5.5em' }} />
          <col style={{ width: '4em' }} /><col style={{ width: '4em' }} />
          <col style={{ width: '8.5em' }} />
        </colgroup>
        <thead>
          <tr>
            <th>信源</th>
            <th>一手性</th>
            <th>通道</th>
            <th className="num">命中</th>
            <th className="num">采纳</th>
            <th>采纳率</th>
          </tr>
        </thead>
        <tbody>
          {d.results.map((s) => (
            <tr key={s.id} className={s.hits ? '' : 'faded'}>
              <td className="src-name" title={s.name}>
                {s.name}
                {s.affiliated_with && <span className="pill warn mini">自家媒体</span>}
                {!s.active && <span className="pill mini">已停用</span>}
              </td>
              <td className="nowrap" title={CLASS_LABEL[s.source_class]}>
                <span className={`cls c${s.source_class}`}>{s.source_class}</span>
                <span className="d"> {CLASS_SHORT[s.source_class]}</span>
              </td>
              <td><span className="chan">{s.channel}</span></td>
              <td className="num">{s.hits || '—'}</td>
              <td className="num">{s.accepted || '—'}</td>
              <td>
                {s.accept_rate == null ? <span className="d">—</span> : (
                  <span className="rate">
                    <span className="rate-bar">
                      <i style={{ width: `${Math.round(s.accept_rate * 100)}%` }} />
                    </span>
                    <span className="num d">{Math.round(s.accept_rate * 100)}%</span>
                  </span>
                )}
              </td>
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
