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
    <div className="view">
      <p className="sec-title">
        {rid || '—'}{incremental && ' · 增量轮'}
      </p>

      <div className="funnel">
        {stages.map((st) => (
          <button key={st.label} className="stage-card"
                  disabled={!st.to || !rid}
                  onClick={() => nav(`/runs/${rid}/rejects`)}>
            <span className="k">{st.label}</span>
            <span className="v">{st.val}</span>
            <span className="d">{st.note}</span>
            {st.to && <span className="more">明细 →</span>}
          </button>
        ))}
      </div>

      <p className="sec-title mt">知识库累计</p>
      <div className="statrow">
        <Stat k="事件" v={meta.counts.events} />
        <Stat k="原始条目" v={meta.counts.items} />
        <Stat k="事实/推断/建议" v={meta.counts.claims} />
        <Stat k="关系边" v={meta.counts.edges} />
        <Stat k="拒绝台账" v={meta.counts.rejects} />
        <Stat k="待人工复核" v={v.pending_review ?? 0} />
      </div>

      <p className="sec-title mt">本轮成本</p>
      <div className="statrow">
        <Stat k="采集（Exa）" v={`$${run?.cost?.total ?? 0}`} />
        <Stat k="模型（DeepSeek）" v={`¥${s.llm_cost_cny ?? 0}`} />
        <Stat k="LLM 调用" v={s.llm_usage?.calls ?? 0} />
        <Stat k="缓存命中" v={s.llm_usage?.cache_hits ?? 0} />
      </div>

      <p className="sec-title mt">知识库构成</p>
      <div className="dist">
        {['官方确认·多源印证', '官方一手', '多源已验证', '深度单源', '单源待确认']
          .filter((k) => v[k]).map((k) => <span key={k}>{k} <b>{v[k]}</b></span>)}
      </div>
      <div className="dist">
        {['新增', '延续', '静默', '旧闻/背景']
          .filter((k) => v[k]).map((k) => <span key={k}>{k} <b>{v[k]}</b></span>)}
      </div>

    </div>
  )
}

const Stat = ({ k, v }) => (
  <div><div className="k">{k}</div><div className="v">{v}</div></div>
)
