import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, offline, onOffline } from './api.js'
import { Collapse, Loading } from './components/Bits.jsx'
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

const Tab = ({ on, go, n, innerRef, children }) => (
  <button ref={innerRef} className={`tab ${on ? 'on' : ''}`} onClick={go}>
    {children}{n != null && <span className="n">{n}</span>}
  </button>
)

/**
 * 顶部导航的滑动下划线。
 *
 * 量出当前标签的位置写进 CSS 变量，让一条共用的墨条滑过去，而不是让每个
 * 标签各自开关自己的 border——后者切换时是硬跳，看不出「从哪来到哪去」。
 *
 * 三个必须重量的时机：
 *   1. 选中项变了
 *   2. 字体加载完 —— 标题字是 woff2 子集，落地前后标签宽度不一样，
 *      只在 mount 时量会让墨条停在错位置
 *   3. 容器尺寸变化 —— 窄屏下 .tabs 会横向滚动，宽度变了位置也就变了
 *
 * 量到了才给容器加 .inked（由它把静态 border 隐掉）。JS 没跑或量失败时
 * 保留原来的 border，不会出现哪个标签都没有下划线的情况。
 */
function useTabInk(active) {
  const box = useRef(null)
  const tabs = useRef({})
  const [inked, setInked] = useState(false)

  const measure = useCallback(() => {
    const el = tabs.current[active]
    const wrap = box.current
    if (!el || !wrap || !el.offsetWidth) return
    wrap.style.setProperty('--ink-x', `${el.offsetLeft}px`)
    wrap.style.setProperty('--ink-w', `${el.offsetWidth}px`)
    setInked(true)
  }, [active])

  useLayoutEffect(() => {
    measure()
    // 字体是 font-display: swap，落地时机不确定，ready 之后再量一次
    document.fonts?.ready.then(measure).catch(() => {})
    const ro = new ResizeObserver(measure)
    if (box.current) ro.observe(box.current)
    return () => ro.disconnect()
  }, [measure])

  return { box, tabs, inked }
}

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
  else if (!meta) body = <Loading />
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

  const tabKey = path.startsWith('/events') ? 'events'
    : path.startsWith('/sources') ? 'sources'
    : path.startsWith('/report') ? 'report'
    : path.startsWith('/brief') ? 'brief'
    : path.startsWith('/docs') ? 'docs' : 'run'
  const ink = useTabInk(tabKey)

  return (
    <div className="wrap">
      <header className="site-header">
        <div className="header-row">
          <div>
            <h1 onClick={() => nav('/')}>行业与竞对 AI 洞察情报站</h1>
          </div>
          {/* 桌面常驻横排；窄屏由 CSS 换成下面的折叠菜单 */}
          <nav className={`tabs ${ink.inked ? 'inked' : ''}`} ref={ink.box}>
            {VIEWS.map((v) => (
              <Tab key={v.k} on={tabKey === v.k} go={() => nav(v.to)}
                   innerRef={(el) => { ink.tabs.current[v.k] = el }}
                   n={v.n ? meta?.counts[v.n] : undefined}>{v.label}</Tab>
            ))}
            <span className="tab-ink" aria-hidden />
          </nav>
          <Collapse className="nav-collapse" label="视图"
                    current={VIEWS.find((v) => v.k === tabKey)?.label || '运行回放'}>
            {VIEWS.map((v) => (
              <button key={v.k} className={`cb-item ${tabKey === v.k ? 'on' : ''}`}
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
