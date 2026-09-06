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

      {rev && rev.total > 0 && (
        <>
          <h2 className="section-title mt">
            人工复核记录 · {rev.results.filter((r) => r.reviewer !== 'system').length} 次操作
          </h2>
          <p className="lede">
            流水线刻意保守——单源不自动采信、疑似重复不自动合并——代价是留下需要人判断的
            队列。复核在本地 CLI 完成，<strong>记录随数据一同版本化并在此展示</strong>，
            每次操作可追溯。
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
                  <td className="dim sm">{r.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}
