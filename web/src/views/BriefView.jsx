import React, { useRef, useState } from 'react'
import { ThinkingOrb } from 'thinking-orbs'

// 步骤 → orb 动画状态。让动画语义对应真实阶段，而不是当装饰用：
// searching 是扫描地球仪、connecting 是连线成星座、composing 是起伏的绸带。
const STEPS = [
  { k: 'analyze',  t: '分析参数', orb: 'working' },
  { k: 'retrieve', t: '检索事件', orb: 'searching' },
  { k: 'facts',    t: '提取事实', orb: 'connecting' },
  { k: 'compose',  t: '生成洞察', orb: 'weaving' },
  { k: 'write',    t: '撰写正文', orb: 'composing' },
]

export default function BriefView({ meta, nav }) {
  const [companies, setCompanies] = useState([])
  const [domains, setDomains] = useState([])
  const [prompt, setPrompt] = useState('')
  const [state, setState] = useState('idle')   // idle | running | done | error
  const [steps, setSteps] = useState([])
  const [text, setText] = useState('')
  const [cited, setCited] = useState([])
  const [err, setErr] = useState(null)
  const [elapsed, setElapsed] = useState(0)
  const [expanded, setExpanded] = useState(true)
  const abort = useRef(null)

  const toggle = (arr, set) => (v) =>
    set(arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v])

  async function run() {
    if (prompt.trim().length < 2 || state === 'running') return
    setState('running'); setSteps([]); setText(''); setCited([]); setErr(null)
    setExpanded(true)
    const ctrl = new AbortController()
    abort.current = ctrl

    try {
      const r = await fetch('/api/v1/briefs/generate', {
        method: 'POST', signal: ctrl.signal,
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ companies, domains, prompt: prompt.trim() }),
      })
      if (!r.ok) {
        const b = await r.json().catch(() => ({}))
        throw new Error(b.detail || `HTTP ${r.status}`)
      }

      const reader = r.body.getReader()
      const dec = new TextDecoder()
      let buf = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        const parts = buf.split('\n\n')
        buf = parts.pop() || ''
        for (const block of parts) {
          const ev = /^event: (.+)$/m.exec(block)?.[1]
          const dt = /^data: (.+)$/m.exec(block)?.[1]
          if (!ev || !dt) continue
          const d = JSON.parse(dt)
          if (ev === 'thinking') setSteps((s) => [...s, d])
          else if (ev === 'sources') setCited(d.events || [])
          else if (ev === 'token') setText((t) => t + d.t)
          else if (ev === 'done') { setElapsed(d.elapsed_ms); setState('done'); setExpanded(false) }
          else if (ev === 'error') { setErr(d.message); setState('error') }
        }
      }
      setState((s) => (s === 'running' ? 'done' : s))
    } catch (e) {
      if (e.name !== 'AbortError') { setErr(String(e.message || e)); setState('error') }
    }
  }

  const curStep = steps.length ? steps[steps.length - 1] : null

  return (
    <div className="view">
      <p className="sec-title">定制简报</p>

      <div className="filterbar">
        <FilterRow label="公司" opts={meta.companies.map((c) => [c.id, c.name])}
                   cur={companies} on={toggle(companies, setCompanies)} />
        <FilterRow label="领域" opts={meta.domains.map((d) => [d.id, d.name])}
                   cur={domains} on={toggle(domains, setDomains)} />
      </div>

      <textarea className="prompt" rows={3}
        placeholder="例：对比各平台 AI 导购的落地阶段差异，哪些做法对我方商家工具有借鉴价值？"
        value={prompt} onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) run() }} />

      <div className="brief-bar">
        <button className="btn" onClick={run}
                disabled={state === 'running' || prompt.trim().length < 2}>
          {state === 'running' ? '生成中…' : '生成简报'}
        </button>
        <span className="d">⌘/Ctrl + Enter</span>
      </div>

      {(state !== 'idle') && (
        <div className="think">
          <button className="think-head" onClick={() => setExpanded(!expanded)}>
            {state === 'running'
              ? <>
                  <ThinkingOrb size={20} theme="light"
                    state={STEPS.find((x) => x.k === curStep?.step)?.orb || 'working'} />
                  <span className="shimmer">{curStep?.text || '准备中'}</span>
                </>
              : <span className="d">
                  思考过程 · {steps.length} 步 · 耗时 {(elapsed / 1000).toFixed(1)}s
                  {expanded ? ' ▲' : ' ▼'}
                </span>}
          </button>
          {expanded && (
            <ol className="steps">
              {STEPS.map((s) => {
                const hit = steps.find((x) => x.step === s.k)
                const active = curStep?.step === s.k && state === 'running'
                return (
                  <li key={s.k} data-done={!!hit} data-active={active}>
                    {hit?.text || s.t}
                  </li>
                )
              })}
            </ol>
          )}
          {cited.length > 0 && (
            <div className="evi">
              <span className="d">引用事件 {cited.length} 个：</span>
              {cited.map((id) => (
                <button key={id} className="ev" onClick={() => nav(`/events/${id}`)}>{id}</button>
              ))}
            </div>
          )}
        </div>
      )}

      {err && <div className="error">{err}</div>}
      {text && <article className="brief-body">{renderMarkdown(text, nav)}</article>}
    </div>
  )
}

function FilterRow({ label, opts, cur, on }) {
  return (
    <div className="frow">
      <span className="flabel">{label}</span>
      {opts.map(([v, name]) => (
        <button key={v} className="fopt" aria-pressed={cur.includes(v)} onClick={() => on(v)}>
          {name}
        </button>
      ))}
      {cur.length === 0 && <span className="d">未选＝全部</span>}
    </div>
  )
}

/** 极简 Markdown 渲染：标题 / 列表 / 表格 / 加粗 / EV 编号可点 */
function renderMarkdown(md, nav) {
  const out = []
  const lines = md.split('\n')
  let list = null
  let table = null

  const flush = () => {
    if (list) { out.push(<ul key={out.length}>{list}</ul>); list = null }
    if (table) {
      const [head, ...rows] = table
      out.push(
        <table className="tbl compact" key={out.length}>
          <thead><tr>{head.map((c, i) => <th key={i}>{inline(c, nav)}</th>)}</tr></thead>
          <tbody>{rows.map((r, i) =>
            <tr key={i}>{r.map((c, j) => <td key={j}>{inline(c, nav)}</td>)}</tr>)}
          </tbody>
        </table>)
      table = null
    }
  }

  for (const raw of lines) {
    const l = raw.trimEnd()
    if (/^\|(.+)\|$/.test(l)) {
      const cells = l.slice(1, -1).split('|').map((c) => c.trim())
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue
      ;(table ||= []).push(cells)
      continue
    }
    flush()
    if (/^#{1,3}\s/.test(l)) {
      const lvl = l.match(/^#+/)[0].length
      const txt = l.replace(/^#+\s*/, '')
      out.push(lvl <= 2
        ? <h3 className="sub-title" key={out.length}>{inline(txt, nav)}</h3>
        : <h4 key={out.length}>{inline(txt, nav)}</h4>)
    } else if (/^[-*]\s/.test(l)) {
      ;(list ||= []).push(<li key={list?.length || 0}>{inline(l.replace(/^[-*]\s/, ''), nav)}</li>)
    } else if (l.trim()) {
      out.push(<p key={out.length}>{inline(l, nav)}</p>)
    }
  }
  flush()
  return out
}

function inline(s, nav) {
  const parts = []
  let i = 0
  const re = /(\*\*(.+?)\*\*)|(EV-[0-9a-f]{8})/g
  let m
  while ((m = re.exec(s))) {
    if (m.index > i) parts.push(s.slice(i, m.index))
    if (m[2]) parts.push(<b key={m.index}>{m[2]}</b>)
    else parts.push(
      <button className="ev" key={m.index} onClick={() => nav(`/events/${m[3]}`)}>{m[3]}</button>)
    i = m.index + m[0].length
  }
  if (i < s.length) parts.push(s.slice(i))
  return parts
}
