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

      {/* 概览条：一手性与通道的分布，直接回应「信息源是否多元」 */}
      <div className="src-overview">
        <div className="ov-group">
          <span className="ov-k">一手性</span>
          {['A', 'B', 'C', 'D', 'E'].map((c) => {
            const n = d.results.filter((s) => s.source_class === c).length
            return n ? (
              <span className="ov-item" key={c}>
                <span className={`cls c${c}`}>{c}</span>
                <span className="d">{CLASS_SHORT[c]}</span><b>{n}</b>
              </span>
            ) : null
          })}
        </div>
        <div className="ov-group">
          <span className="ov-k">通道</span>
          {Object.entries(d.by_channel).filter(([, n]) => n).map(([k, n]) => (
            <span className="ov-item" key={k}>
              <span className="chan">{k}</span><b>{n}</b>
            </span>
          ))}
        </div>
      </div>

      <table className="tbl src-tbl">
        <colgroup>
          <col style={{ width: '24%' }} /><col style={{ width: '15%' }} />
          <col style={{ width: '10%' }} /><col style={{ width: '8%' }} />
          <col style={{ width: '8%' }} /><col style={{ width: '17%' }} />
          <col style={{ width: '18%' }} />
        </colgroup>
        <thead>
          <tr>
            <th>信源</th>
            <th>一手性</th>
            <th>通道</th>
            <th className="num">命中</th>
            <th className="num">采纳</th>
            <th>采纳率</th>
            <th>最近命中</th>
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
              <td className="tnum d">
                {s.last_hit ? fmtDate(s.last_hit) : <span className="d">未命中</span>}
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
          <table className="tbl rev-tbl">
            <colgroup>
              <col style={{ width: '7.5em' }} />
              <col style={{ width: '8em' }} />
              <col style={{ width: '9em' }} />
              <col />
            </colgroup>
            <thead>
              <tr>
                <th>时间</th><th>操作</th><th>对象</th><th>依据</th>
              </tr>
            </thead>
            <tbody>
              {rev.results.filter((r) => r.reviewer !== 'system').map((r, i) => (
                <tr key={i}>
                  {/* 时间与对象靠左：早先误用了 .num 类，而 .num 带
                      text-align: right，把这两列推到了右边 */}
                  <td className="tnum d nowrap">{fmtDate(r.created_at)}</td>
                  <td className="nowrap">{ACTION_LABEL[r.action] || r.action}</td>
                  <td className="tnum d">
                    {String(r.target_id || '').split('|').filter(Boolean).map((id) => (
                      <span className="evline" key={id}>{id}</span>
                    ))}
                  </td>
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
