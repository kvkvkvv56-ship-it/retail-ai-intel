import React from 'react'

export default function RunView({ meta, run, nav }) {
  const s = run?.stats || {}
  const v = s.verify || {}
  const rid = run?.id

  // 五格必须全部是「本轮」口径。早期版本最后一格错用了知识库累计事件数，
  // 增量轮会出现「440 条原始 → 27 个事件」这种对不上的数字。
  const stages = [
    { label: '采集原始条目', val: s.raw ?? 0, note: '四通道汇总', to: null },
    { label: '去重后',
      val: (s.raw ?? 0) - (s.duplicate_url ?? 0) - (s.stale ?? 0),
      note: `URL 重复 −${s.duplicate_url ?? 0}｜超窗 −${s.stale ?? 0}`,
      to: 'duplicate_url' },
    { label: '过关键词粗筛', val: s.accepted ?? 0,
      note: `无关 −${s.off_topic ?? 0}｜转载 −${s.syndication ?? 0}`,
      to: 'off_topic' },
    { label: '过 LLM 预筛', val: s.prescreen?.keep ?? 0,
      note: `判无关 −${s.prescreen?.drop ?? 0}`, to: 'irrelevant' },
    { label: '本轮新增事件', val: s.merge?.events ?? 0,
      note: `合并掉 ${s.merge?.merged_away ?? 0} 条重复报道`, to: 'merged' },
  ]
  const incremental = (s.duplicate_url ?? 0) > 0

  return (
    <section>
      <h2 className="section-title">
        本轮运行 · {rid || '—'}
        {incremental && <span className="tag">增量轮</span>}
      </h2>
      <p className="lede">
        从公开信息到可追溯事件的完整链路。每一格都可下钻到明细，
        <strong>包括被淘汰的条目和各自的淘汰理由</strong>——识别出旧闻与转载本身就是产出，
        悄悄丢掉等于没做。
      </p>

      <div className="funnel">
        {stages.map((st, i) => (
          <button key={st.label} className="stage" data-dim={i > 0}
                  disabled={!st.to || !rid}
                  onClick={() => nav(`/runs/${rid}/rejects`)}>
            <span className="stage-label">{st.label}</span>
            <span className="stage-val">{st.val}</span>
            <span className="stage-note">{st.note}</span>
            {st.to && <span className="stage-more">查看明细 →</span>}
          </button>
        ))}
      </div>

      <h2 className="section-title mt">知识库累计</h2>
      <div className="stats">
        <Stat k="事件" v={meta.counts.events} />
        <Stat k="原始条目" v={meta.counts.items} />
        <Stat k="事实/推断/建议" v={meta.counts.claims} />
        <Stat k="关系边" v={meta.counts.edges} />
        <Stat k="拒绝台账" v={meta.counts.rejects} />
        <Stat k="待人工复核" v={v.pending_review ?? 0} />
      </div>

      <h2 className="section-title mt">本轮成本</h2>
      <div className="stats">
        <Stat k="采集（Exa）" v={`$${run?.cost?.total ?? 0}`} />
        <Stat k="模型（DeepSeek）" v={`¥${s.llm_cost_cny ?? 0}`} />
        <Stat k="LLM 调用" v={s.llm_usage?.calls ?? 0} />
        <Stat k="缓存命中" v={s.llm_usage?.cache_hits ?? 0} />
      </div>

      <h2 className="section-title mt">知识库构成</h2>
      <div className="dist">
        {['官方确认·多源印证', '官方一手', '多源已验证', '深度单源', '单源待确认']
          .filter((k) => v[k]).map((k) => <span key={k}>{k} <b>{v[k]}</b></span>)}
      </div>
      <div className="dist">
        {['新增', '延续', '静默', '旧闻/背景']
          .filter((k) => v[k]).map((k) => <span key={k}>{k} <b>{v[k]}</b></span>)}
      </div>
      {v.backdated > 0 && (
        <p className="dim sm mt-s">
          其中 {v.backdated} 个事件的 event_date 早于观察窗口，判为旧闻而非新增——
          报道可能是新的，但讲的是历史事件。
        </p>
      )}
    </section>
  )
}

const Stat = ({ k, v }) => (
  <div><div className="stat-label">{k}</div><div className="stat-val">{v}</div></div>
)
