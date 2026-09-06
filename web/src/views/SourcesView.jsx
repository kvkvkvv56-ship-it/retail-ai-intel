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

      {/* 只列名字，按一手性分组横排。
          采集量、采纳率是内部运维指标——摊在页面上会把「这套信源覆盖了谁」
          这条真正的信息淹掉；分级本身才是这套体系的说明。 */}
      <div className="src-groups">
        {['A', 'B', 'C', 'D', 'E'].map((c) => {
          const g = d.results.filter((s) => s.source_class === c)
          if (!g.length) return null
          return (
            <section className="src-group" key={c}>
              <div className="sg-head">
                <span className={`cls c${c}`}>{c}</span>
                <span className="sg-name">{CLASS_LABEL[c] || CLASS_SHORT[c]}</span>
                <span className="sg-n tnum">{g.length}</span>
              </div>
              <div className="sg-items">
                {g.map((s) => (
                  <span key={s.id} className={`src-chip ${s.active ? '' : 'off'}`}>
                    {s.name}
                    {/* A 类是当事方自己的渠道，标「自家媒体」是同义反复；
                        这个标记只在媒体上才有信息量——比如天下网商报道阿里 */}
                    {s.affiliated_with && c !== 'A' && <i>自家媒体</i>}
                    {!s.active && <i>已停用</i>}
                  </span>
                ))}
              </div>
            </section>
          )
        })}
      </div>

      {rev && rev.total > 0 && (
        <>
          <p className="sec-title mt">
            人工复核记录 · {rev.total} 次操作，影响 {rev.affected_events} 个事件
          </p>
          {/* 按操作类型先给聚合，再列最近几条。逐条列出会把有效信息淹没在
              重复里——尤其批量裁决之后 */}
          <div className="dist" style={{ marginBottom: 14 }}>
            {Object.entries(rev.by_action || {}).map(([k, n]) => (
              <span key={k}>{ACTION_LABEL[k] || k} <b>{n}</b></span>
            ))}
          </div>
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
              {rev.results.map((r, i) => (
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
          {rev.truncated > 0 && (
            <p className="empty">
              仅显示最近 {rev.results.length} 条，另有 {rev.truncated} 条见仓库
              <code> data/reviews.jsonl</code>
            </p>
          )}
        </>
      )}
    </div>
  )
}
