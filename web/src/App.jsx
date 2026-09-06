import React, { useEffect, useState } from 'react'

const api = (p) => fetch(`/api/v1/${p}`).then((r) => {
  if (!r.ok) throw new Error(`${r.status} ${p}`)
  return r.json()
})

export default function App() {
  const [meta, setMeta] = useState(null)
  const [run, setRun] = useState(null)
  const [err, setErr] = useState(null)

  useEffect(() => {
    api('meta')
      .then((m) => {
        setMeta(m)
        return m.latest_run ? api(`runs/${m.latest_run}`) : null
      })
      .then(setRun)
      .catch((e) => setErr(String(e)))
  }, [])

  if (err) return <Shell><div className="error">加载失败：{err}</div></Shell>
  if (!meta) return <Shell><div className="loading">载入中…</div></Shell>

  const s = run?.stats || {}
  const v = s.verify || {}
  const stages = [
    { label: '采集原始条目', val: s.raw ?? 0, note: '四通道汇总' },
    { label: '去重后', val: (s.raw ?? 0) - (s.duplicate_url ?? 0) - (s.stale ?? 0),
      note: `URL 重复 −${s.duplicate_url ?? 0}｜超窗 −${s.stale ?? 0}` },
    { label: '过粗筛', val: s.accepted ?? 0,
      note: `无关 −${s.off_topic ?? 0}｜转载 −${s.syndication ?? 0}` },
    { label: '过 LLM 预筛', val: s.prescreen?.keep ?? 0,
      note: `判无关 −${s.prescreen?.drop ?? 0}` },
    { label: '本轮新增事件', val: s.merge?.events ?? 0,
      note: `合并掉 ${s.merge?.merged_away ?? 0} 条重复报道` },
  ]

  // 漏斗五格必须全部是「本轮」口径。早期版本最后一格错用了知识库累计事件数，
  // 增量轮会出现「440 条原始 → 27 个事件」这种对不上的数字。
  const incremental = (s.duplicate_url ?? 0) > 0

  return (
    <Shell meta={meta}>
      <section>
        <h2 className="section-title">
          本轮运行 · {run?.id || '—'}
          {incremental && <span className="tag">增量轮</span>}
        </h2>
        <p className="lede">
          从公开信息到可追溯事件的完整链路。每一格都可下钻到明细，
          <strong>包括被淘汰的条目和各自的淘汰理由</strong>——识别出旧闻与转载本身就是产出，
          悄悄丢掉等于没做。
        </p>

        <div className="funnel">
          {stages.map((st, i) => (
            <button key={st.label} className="stage" data-dim={i > 0}>
              <span className="stage-label">{st.label}</span>
              <span className="stage-val">{st.val}</span>
              <span className="stage-note">{st.note}</span>
            </button>
          ))}
        </div>

        <h2 className="section-title" style={{ marginTop: 'calc(var(--u) * 6)' }}>
          知识库累计
        </h2>
        <div className="stats">
          <div>
            <div className="stat-label">事件</div>
            <div className="stat-val">{meta.counts.events}</div>
          </div>
          <div>
            <div className="stat-label">原始条目</div>
            <div className="stat-val">{meta.counts.items}</div>
          </div>
          <div>
            <div className="stat-label">事实 / 推断 / 建议</div>
            <div className="stat-val">{meta.counts.claims}</div>
          </div>
          <div>
            <div className="stat-label">拒绝台账</div>
            <div className="stat-val">{meta.counts.rejects}</div>
          </div>
          <div>
            <div className="stat-label">待人工复核</div>
            <div className="stat-val">{v.pending_review ?? 0}</div>
          </div>
        </div>

        <h2 className="section-title" style={{ marginTop: 'calc(var(--u) * 6)' }}>
          本轮成本
        </h2>
        <div className="stats">
          <div>
            <div className="stat-label">采集成本</div>
            <div className="stat-val">${run?.cost?.total ?? 0}</div>
          </div>
          <div>
            <div className="stat-label">模型成本</div>
            <div className="stat-val">¥{s.llm_cost_cny ?? 0}</div>
          </div>
          <div>
            <div className="stat-label">LLM 调用</div>
            <div className="stat-val">{s.llm_usage?.calls ?? 0}</div>
          </div>
          <div>
            <div className="stat-label">缓存命中</div>
            <div className="stat-val">{s.llm_usage?.cache_hits ?? 0}</div>
          </div>
        </div>

        <h2 className="section-title" style={{ marginTop: 'calc(var(--u) * 6)' }}>
          知识库构成
        </h2>
        <div className="dist">
          {['官方确认·多源印证', '官方一手', '多源已验证', '深度单源', '单源待确认']
            .filter((k) => v[k])
            .map((k) => <span key={k}>{k} <b>{v[k]}</b></span>)}
        </div>
        <div className="dist">
          {['新增', '延续', '静默', '旧闻/背景']
            .filter((k) => v[k])
            .map((k) => <span key={k}>{k} <b>{v[k]}</b></span>)}
        </div>
      </section>
    </Shell>
  )
}

function Shell({ meta, children }) {
  return (
    <div className="shell">
      <header className="masthead">
        <h1>行业与竞对 AI 洞察助手</h1>
        <span className="sub">
          {meta ? `${meta.name}｜观察窗口 ${meta.window_days} 天` : ''}
        </span>
        <span className="spacer" />
        <nav className="tabs">
          <button aria-current="true">运行回放</button>
          <button>事件库</button>
          <button>周报</button>
          <button>信源</button>
        </nav>
      </header>
      {children}
      <footer className="colophon">
        <span>只采集公开可访问信息，遵守 robots.txt</span>
        <span>正文仅保留截断摘录与原文链接</span>
        <span>输出供内部研究参考，不构成投资建议</span>
        {meta && <span>快照 {meta.generated_at?.slice(0, 16).replace('T', ' ')} UTC</span>}
      </footer>
    </div>
  )
}
