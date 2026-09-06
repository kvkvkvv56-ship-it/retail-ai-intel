import React, { useEffect, useState } from 'react'
import { api, CLASS_LABEL } from '../api.js'

/**
 * 信源健康度（技术方案 §3.4）
 * 直接对应「信息源是否多元、可靠」的评分说明，也是汰换信源的依据。
 */
export default function SourcesView() {
  const [d, setD] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => { api('sources').then(setD).catch((e) => setErr(String(e))) }, [])
  if (err) return <div className="error">加载失败：{err}</div>
  if (!d) return <div className="loading">载入中…</div>

  return (
    <section>
      <h2 className="section-title">信源体系 · {d.total} 个</h2>
      <p className="lede">
        分层轴是<strong>一手性</strong>——这条信息离事件现场有多近，而非发布主体的身份。
        晚点的独家报道比公司的 AI 公关稿更接近一手。
        通道分布：{Object.entries(d.by_channel).map(([k, v]) => `${k} ${v}`).join(' · ')}。
      </p>
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
              <td className="dim">{s.source_class} {CLASS_LABEL[s.source_class]}</td>
              <td className="dim">{s.channel}</td>
              <td className="num">{s.hits}</td>
              <td className="num">{s.accepted}</td>
              <td className="num dim">
                {s.accept_rate == null ? '—' : `${Math.round(s.accept_rate * 100)}%`}
              </td>
              <td className="dim sm">{s.note || ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
