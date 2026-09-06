import React, { useEffect, useState } from 'react'
import { api } from './api.js'
import RunView from './views/RunView.jsx'
import EventsView from './views/EventsView.jsx'
import EventDetail from './views/EventDetail.jsx'
import RejectsView from './views/RejectsView.jsx'
import SourcesView from './views/SourcesView.jsx'
import ReportView from './views/ReportView.jsx'
import BriefView from './views/BriefView.jsx'

export default function App() {
  const [meta, setMeta] = useState(null)
  const [run, setRun] = useState(null)
  const [err, setErr] = useState(null)
  const [path, setPath] = useState(window.location.pathname)

  useEffect(() => {
    const onPop = () => setPath(window.location.pathname)
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const nav = (to) => {
    window.history.pushState({}, '', to)
    setPath(to)
    window.scrollTo(0, 0)
  }

  useEffect(() => {
    api('meta')
      .then((m) => { setMeta(m); return m.latest_run ? api(`runs/${m.latest_run}`) : null })
      .then(setRun)
      .catch((e) => setErr(String(e)))
  }, [])

  let body
  if (err) body = <div className="error">加载失败：{err}</div>
  else if (!meta) body = <div className="loading">载入中…</div>
  else {
    const ev = path.match(/^\/events\/([A-Za-z0-9\-]+)$/)
    const rj = path.match(/^\/runs\/([A-Za-z0-9\-]+)\/rejects$/)
    if (ev) body = <EventDetail id={ev[1]} meta={meta} nav={nav} />
    else if (rj) body = <RejectsView runId={rj[1]} nav={nav} />
    else if (path.startsWith('/events')) body = <EventsView meta={meta} nav={nav} />
    else if (path.startsWith('/sources')) body = <SourcesView />
    else if (path.startsWith('/report')) body = <ReportView meta={meta} nav={nav} />
    else if (path.startsWith('/brief')) body = <BriefView meta={meta} nav={nav} />
    else body = <RunView meta={meta} run={run} nav={nav} />
  }

  const tab = path.startsWith('/events') ? 'events'
    : path.startsWith('/sources') ? 'sources'
    : path.startsWith('/report') ? 'report'
    : path.startsWith('/brief') ? 'brief' : 'run'

  return (
    <div className="shell">
      <header className="masthead">
        <h1><a href="/" onClick={(e) => { e.preventDefault(); nav('/') }}>
          行业与竞对 AI 洞察助手
        </a></h1>
        <span className="sub">{meta ? `${meta.name}｜观察窗口 ${meta.window_days} 天` : ''}</span>
        <span className="spacer" />
        <nav className="tabs">
          <button aria-current={tab === 'run'} onClick={() => nav('/')}>运行回放</button>
          <button aria-current={tab === 'events'} onClick={() => nav('/events')}>事件库</button>
          <button aria-current={tab === 'report'} onClick={() => nav('/report')}>周报</button>
          <button aria-current={tab === 'brief'} onClick={() => nav('/brief')}>定制简报</button>
          <button aria-current={tab === 'sources'} onClick={() => nav('/sources')}>信源</button>
        </nav>
      </header>
      {body}
      <footer className="colophon">
        <span>只采集公开可访问信息，遵守 robots.txt</span>
        <span>正文仅保留截断摘录与原文链接</span>
        <span>输出供内部研究参考，不构成投资建议</span>
        {meta && <span>快照 {meta.generated_at?.slice(0, 16).replace('T', ' ')} UTC</span>}
      </footer>
    </div>
  )
}
