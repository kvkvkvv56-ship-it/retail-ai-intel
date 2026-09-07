import React, { useEffect, useState } from 'react'
import { api, offline, onOffline } from './api.js'
import { Collapse } from './components/Bits.jsx'
import RunView from './views/RunView.jsx'
import EventsView from './views/EventsView.jsx'
import EventDetail from './views/EventDetail.jsx'
import RejectsView from './views/RejectsView.jsx'
import SourcesView from './views/SourcesView.jsx'
import ReportView from './views/ReportView.jsx'
import ReportsIndex from './views/ReportsIndex.jsx'
import BriefView from './views/BriefView.jsx'
import DocsView from './views/DocsView.jsx'

const VIEWS = [
  { k: 'run', to: '/', label: '运行回放' },
  { k: 'events', to: '/events', label: '事件库', n: 'events' },
  { k: 'report', to: '/report', label: '周报' },
  { k: 'brief', to: '/brief', label: '定制简报' },
  { k: 'sources', to: '/sources', label: '信源', n: 'sources' },
  { k: 'docs', to: '/docs', label: '文档' },
]

const Tab = ({ on, go, n, children }) => (
  <button className={`tab ${on ? 'on' : ''}`} onClick={go}>
    {children}{n != null && <span className="n">{n}</span>}
  </button>
)

export default function App() {
  const [meta, setMeta] = useState(null)
  const [run, setRun] = useState(null)
  const [err, setErr] = useState(null)
  const [path, setPath] = useState(window.location.pathname)
  const [isOffline, setOffline] = useState(offline.on)

  useEffect(() => onOffline(() => setOffline(true)), [])

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
    const rp = path.match(/^\/report\/([A-Za-z0-9\-]+)$/)
    const dc = path.match(/^\/docs\/([a-z0-9\-]+)$/)
    if (ev) body = <EventDetail id={ev[1]} meta={meta} nav={nav} />
    else if (rj) body = <RejectsView runId={rj[1]} nav={nav} />
    else if (path.startsWith('/events')) body = <EventsView meta={meta} nav={nav} />
    else if (path.startsWith('/sources')) body = <SourcesView />
    else if (path.startsWith('/reports')) body = <ReportsIndex nav={nav} />
    else if (rp) body = <ReportView meta={meta} nav={nav} id={rp[1]} />
    else if (path.startsWith('/report')) body = <ReportView meta={meta} nav={nav} />
    else if (dc) body = <DocsView id={dc[1]} nav={nav} />
    else if (path.startsWith('/docs')) body = <DocsView nav={nav} />
    else if (path.startsWith('/brief')) body = <BriefView meta={meta} nav={nav} />
    else body = <RunView meta={meta} run={run} nav={nav} />
  }

  const tab = path.startsWith('/events') ? 'events'
    : path.startsWith('/sources') ? 'sources'
    : path.startsWith('/report') ? 'report'
    : path.startsWith('/brief') ? 'brief'
    : path.startsWith('/docs') ? 'docs' : 'run'

  return (
    <div className="wrap">
      <header className="site-header">
        <div className="header-row">
          <div>
            <h1 onClick={() => nav('/')}>行业与竞对 AI 洞察情报站</h1>
          </div>
          {/* 桌面常驻横排；窄屏由 CSS 换成下面的折叠菜单 */}
          <nav className="tabs">
            {VIEWS.map((v) => (
              <Tab key={v.k} on={tab === v.k} go={() => nav(v.to)}
                   n={v.n ? meta?.counts[v.n] : undefined}>{v.label}</Tab>
            ))}
          </nav>
          <Collapse className="nav-collapse" label="视图"
                    current={VIEWS.find((v) => v.k === tab)?.label || '运行回放'}>
            {VIEWS.map((v) => (
              <button key={v.k} className={`cb-item ${tab === v.k ? 'on' : ''}`}
                      onClick={() => nav(v.to)}>
                {v.label}
                {v.n && <span className="n">{meta?.counts[v.n]}</span>}
              </button>
            ))}
          </Collapse>
        </div>
      </header>
      {isOffline && (
        <div className="offline-bar">
          离线模式 · 接口暂不可用，正在显示构建时嵌入的快照。事件详情与定制简报需要联网。
        </div>
      )}
      {body}
      <footer className="colophon">
        <span>只采集公开可访问信息，遵守 robots.txt</span>
        <span>正文仅保留截断摘录与原文链接</span>
        <span>输出供内部研究参考，不构成投资建议</span>
        <button className="foot-link" onClick={() => nav('/docs')}>方法与运行说明 →</button>
        {meta && <span>快照 {meta.generated_at?.slice(0, 16).replace('T', ' ')} UTC</span>}
      </footer>
    </div>
  )
}
