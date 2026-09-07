import React, { useEffect, useMemo, useState } from 'react'
import { marked } from 'marked'
import { api } from '../api.js'
import { Collapse, Empty } from '../components/Bits.jsx'

/**
 * 站内文档站。
 *
 * 文档源是 docs/*.md，由 pipeline/docs_export.py 落进 API 快照，
 * 和数据走同一条更新链路——文档不必另开一套发布流程。
 *
 * 渲染放在前端而不是 Python：两侧各写一套 Markdown 规则，迟早会漂移。
 */

/** 标题 → 锚点。中文直接保留，URL fragment 支持。 */
function slug(text, seen) {
  const base = String(text)
    .trim()
    .replace(/[`*_~]/g, '')
    .replace(/[^\w一-鿿]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .toLowerCase() || 'h'
  const n = (seen[base] = (seen[base] || 0) + 1)
  return n === 1 ? base : `${base}-${n}`
}

function render(md) {
  const seen = {}
  const renderer = new marked.Renderer()
  const base = renderer.heading.bind(renderer)
  renderer.heading = function (token) {
    const html = base(token)
    const id = slug(token.text, seen)
    // 标题挂锚点 + 一个 # 链接，长文档要能把某一节直接发给别人
    return html.replace(
      /^<(h[1-6])>/,
      `<$1 id="${id}"><a class="anchor" href="#${id}" aria-label="链接到本节">#</a>`,
    )
  }
  const table = renderer.table.bind(renderer)
  renderer.table = function (token) {
    // 包一层再滚动。直接给 table 设 display:block 会让列宽退化成
    // 收缩到内容宽度，首列被压成一两个字一行（实测「线上地/址」竖排）
    return `<div class="md-table">${table(token)}</div>`
  }
  const link = renderer.link.bind(renderer)
  renderer.link = function (token) {
    const html = link(token)
    // 站内文档互链（形如 方法论.md）改写成前端路由，其余外链新窗口打开
    if (/^https?:/.test(token.href)) {
      return html.replace('<a ', '<a target="_blank" rel="noopener noreferrer" ')
    }
    return html
  }
  return marked.parse(md, { renderer, breaks: false, gfm: true })
}

/** 文档内互链 docs/xxx.md 或 xxx.md → 站内路由 */
const FILE_TO_ID = {
  '使用说明.md': 'guide',
  '作品文档.md': 'overview', '方法论.md': 'methodology', '运行说明.md': 'operations',
  '技术方案.md': 'architecture',
}

export default function DocsView({ id, nav }) {
  const [index, setIndex] = useState(null)
  const [doc, setDoc] = useState(null)
  const [err, setErr] = useState(null)
  const [active, setActive] = useState('')
  // 用**回调 ref 存进 state**，而不是 useRef。
  //
  // index（文档列表）与 doc（单篇内容）是两个独立请求。doc 先到、index 未到时，
  // 组件走 `if (!index) return <loading>` 早退，.md 压根不在树里，ref 是 null；
  // 而这时 html 已经算好，依赖 [html] 的效应跑一次就永久放弃——之后 html
  // 不再变化，效应再也不会重跑。表现就是目录高亮从头到尾一动不动。
  // 存进 state 后，节点挂载这件事本身触发重渲染，效应必然在节点就绪后执行。
  const [bodyEl, setBodyEl] = useState(null)

  useEffect(() => {
    api('docs').then(setIndex).catch((e) => setErr(String(e)))
  }, [])

  const docId = id || index?.results?.[0]?.id

  useEffect(() => {
    if (!docId) return
    setDoc(null)
    api(`docs/${docId}`).then(setDoc).catch((e) => setErr(String(e)))
  }, [docId])

  const html = useMemo(() => (doc ? render(doc.markdown) : ''), [doc])

  // 目录锚点必须和渲染用同一套 slug 规则，所以在这里重算一遍而不是用后端 outline
  const toc = useMemo(() => {
    if (!doc) return []
    const seen = {}
    return doc.outline
      .map((h) => ({ ...h, id: slug(h.text, seen) }))
      .filter((h) => h.level >= 2)
  }, [doc])

  // Markdown 里常用「| | |」写无表头的键值表，渲染出来是一行空 th
  // 加一条分隔线，视觉上像多了个空行
  useEffect(() => {
    if (!html || !bodyEl) return
    bodyEl.querySelectorAll('table').forEach((t) => {
      const th = [...t.querySelectorAll('thead th')]
      if (th.length && th.every((x) => !x.textContent.trim())) {
        t.classList.add('no-head')
      }
    })
  }, [html, bodyEl])

  // 滚动高亮当前小节。
  //
  // 设计原则：**触发与判定分开**。
  //
  // 判定永远是同一套确定性规则——当前小节 = 最后一个顶边越过阈值线的标题，
  // 一个都没越过就取第一个。没有任何分支能强制选中末条：
  // 之前加过一个「滚到底就高亮最后一节」的分支，两次都变成了故障源
  // （高亮被永久钉在末条），它带来的那点观感收益不值这个代价，已删除。
  //
  // 触发用两条并联：window 的 scroll 事件，以及挂在各标题上的
  // IntersectionObserver。前者在文档本身滚动时有效；后者在页面由某个
  // 嵌套容器滚动、window 收不到事件时兜底。IO 在这里只当触发器，
  // 不参与判定——它的 entries 只含状态变化的元素且顺序不保证，
  // 拿它直接选节点正是最初那版的错误。
  useEffect(() => {
    if (!html || !bodyEl) return

    // **每次都从 bodyEl 现查标题，绝不缓存这个数组。**
    //
    // 实测踩过：把 querySelectorAll 的结果缓存进闭包，之后 React 重设了
    // dangerouslySetInnerHTML，.md 这个 div 本身没变（isSameNode 为 true、
    // isConnected 为 true），但它的子节点被整体换掉了——缓存下来的那批标题
    // isConnected 变成 false。对脱离文档的节点 getBoundingClientRect()
    // 恒返回 0，`0 <= 120` 于是永远成立，循环每次都走到末尾，
    // 高亮被永久钉在最后一条。现查十几个节点的代价远小于这个风险。
    const heads = () => [...bodyEl.querySelectorAll('h2[id], h3[id]')]

    const LINE = 120                       // 略低于吸顶头部
    let raf = 0
    const compute = () => {
      raf = 0
      const hs = heads()
      if (!hs.length) return
      let cur = hs[0].id
      for (const h of hs) {
        if (h.getBoundingClientRect().top <= LINE) cur = h.id
        else break
      }
      setActive(cur)
    }
    const kick = () => { if (!raf) raf = requestAnimationFrame(compute) }

    compute()
    // capture 阶段挂在 document 上：嵌套滚动容器的 scroll 不冒泡到 window
    document.addEventListener('scroll', kick, { capture: true, passive: true })
    window.addEventListener('resize', kick)
    // 内容被换掉时重算一次——IO/scroll 都可能错过这个时机
    const mo = new MutationObserver(kick)
    mo.observe(bodyEl, { childList: true, subtree: false })
    return () => {
      if (raf) cancelAnimationFrame(raf)
      document.removeEventListener('scroll', kick, { capture: true })
      window.removeEventListener('resize', kick)
      mo.disconnect()
    }
  }, [html, bodyEl])

  // 长文档的目录自身会滚动，高亮项要保证可见。
  //
  // **不能用 scrollIntoView**：它会滚动所有可滚动祖先，包括文档本身，
  // 那会形成「推动页面 → 触发重算 → 再推动」的正反馈。
  // 这里只写目录容器自己的 scrollTop，物理上不可能影响窗口。
  useEffect(() => {
    if (!active) return
    const box = document.querySelector('.docs-toc')
    const el = box?.querySelector(`a[data-h="${CSS.escape(active)}"]`)
    if (!box || !el || box.scrollHeight <= box.clientHeight) return
    const top = el.offsetTop - box.offsetTop
    const bottom = top + el.offsetHeight
    if (top < box.scrollTop) box.scrollTop = top
    else if (bottom > box.scrollTop + box.clientHeight) {
      box.scrollTop = bottom - box.clientHeight
    }
  }, [active])

  // 文档内互链走前端路由，不整页跳转
  const onBodyClick = (e) => {
    const a = e.target.closest('a')
    if (!a) return
    const href = a.getAttribute('href') || ''
    if (href.startsWith('#')) {
      e.preventDefault()
      const el = bodyEl?.querySelector(`[id="${CSS.escape(href.slice(1))}"]`)
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
      return
    }
    const file = decodeURIComponent(href.split('/').pop() || '')
    if (FILE_TO_ID[file]) {
      e.preventDefault()
      nav(`/docs/${FILE_TO_ID[file]}`)
    }
  }

  if (err) return <div className="error">加载失败：{err}</div>
  if (!index) return <div className="loading">载入中…</div>
  if (!index.results.length) return <Empty>尚无文档</Empty>

  const pos = index.results.findIndex((d) => d.id === docId)
  const prev = pos > 0 ? index.results[pos - 1] : null
  const next = pos >= 0 && pos < index.results.length - 1 ? index.results[pos + 1] : null

  return (
    <div className="view docs-layout">
      {/* 窄屏：文档选择与本页目录都折叠成一行 */}
      <Collapse className="docs-collapse" label="文档"
                current={doc?.title.split('·').pop().trim() || '载入中'}>
        {index.results.map((dd) => (
          <button key={dd.id} className={`cb-item ${dd.id === docId ? 'on' : ''}`}
                  onClick={() => nav(`/docs/${dd.id}`)}>
            <span className="cb-t">{dd.title.split('·').pop().trim()}</span>
            <span className="cb-d">{dd.description}</span>
          </button>
        ))}
      </Collapse>
      {toc.length > 0 && (
        <Collapse className="docs-collapse toc-collapse" label="本页目录"
                  current={toc.find((h) => h.id === active)?.text || toc[0].text}>
          {toc.map((h) => (
            <button key={h.id} className={`cb-item lv${h.level} ${active === h.id ? 'on' : ''}`}
                    onClick={() => bodyEl
                      ?.querySelector(`[id="${CSS.escape(h.id)}"]`)
                      ?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>
              <span className="cb-t">{h.text}</span>
            </button>
          ))}
        </Collapse>
      )}

      {/* ------------------------------- 左：文档目录 */}
      <aside className="docs-nav">
        {index.groups.map((g) => (
          <div className="dn-group" key={g}>
            <p className="dn-k">{g}</p>
            <ul>
              {index.results.filter((d) => d.group === g).map((d) => (
                <li key={d.id}>
                  <button className={`dn-item ${d.id === docId ? 'on' : ''}`}
                          onClick={() => nav(`/docs/${d.id}`)}>
                    <span className="dn-t">{d.title.split('·').pop().trim()}</span>
                    <span className="dn-d">{d.description}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </aside>

      {/* ------------------------------- 中：正文 */}
      <article className="docs-main">
        {!doc ? <div className="loading">载入中…</div> : (
          <>
            <div className="docs-crumb">
              <span>{doc.group}</span>
              <span className="sep">/</span>
              <code>{doc.source}</code>
              {doc.updated && <><span className="sep">/</span><span className="tnum">{doc.updated}</span></>}
            </div>
            <div className="md" ref={setBodyEl} onClick={onBodyClick}
                 dangerouslySetInnerHTML={{ __html: html }} />
            <nav className="docs-pager">
              {prev ? (
                <button onClick={() => nav(`/docs/${prev.id}`)}>
                  <span className="pg-k">← 上一篇</span>
                  <span className="pg-t">{prev.title.split('·').pop().trim()}</span>
                </button>
              ) : <span />}
              {next && (
                <button className="nx" onClick={() => nav(`/docs/${next.id}`)}>
                  <span className="pg-k">下一篇 →</span>
                  <span className="pg-t">{next.title.split('·').pop().trim()}</span>
                </button>
              )}
            </nav>
          </>
        )}
      </article>

      {/* ------------------------------- 右：本页目录 */}
      <aside className="docs-toc">
        {toc.length > 0 && (
          <>
            <p className="dn-k">本页目录</p>
            <ul>
              {toc.map((h) => (
                <li key={h.id} className={`lv${h.level}`}>
                  <a href={`#${h.id}`} data-h={h.id} className={active === h.id ? 'on' : ''}
                     onClick={(e) => {
                       e.preventDefault()
                       bodyEl?.querySelector(`[id="${CSS.escape(h.id)}"]`)
                         ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                     }}>
                    {h.text}
                  </a>
                </li>
              ))}
            </ul>
          </>
        )}
      </aside>
    </div>
  )
}
