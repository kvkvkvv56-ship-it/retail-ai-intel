import React, { useEffect, useMemo, useRef, useState } from 'react'
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
  const bodyRef = useRef(null)

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
    if (!html || !bodyRef.current) return
    bodyRef.current.querySelectorAll('table').forEach((t) => {
      const th = [...t.querySelectorAll('thead th')]
      if (th.length && th.every((x) => !x.textContent.trim())) {
        t.classList.add('no-head')
      }
    })
  }, [html])

  // 滚动高亮当前小节。
  //
  // 原先用 IntersectionObserver 取 entries[0]，但 entries 只含**交叉状态发生变化**
  // 的标题，且顺序不保证是文档序。点目录跳转时页面一次滚过好几屏，中间那些标题
  // 根本不产生 entry，高亮就卡在第一节不动。
  // 改成直接按位置算：当前小节 = 最后一个顶边越过阈值线的标题。确定性，
  // 与滚动方式无关。
  useEffect(() => {
    if (!html || !bodyRef.current) return
    const hs = [...bodyRef.current.querySelectorAll('h2[id], h3[id]')]
    if (!hs.length) return

    const LINE = 120                       // 略低于吸顶头部
    let raf = 0
    const compute = () => {
      raf = 0
      // 滚到底时高亮最后一节：末节太短时永远越不过阈值线。
      // 但必须先确认页面真的能滚——文档短于一屏时 innerHeight 就已经
      // 等于 scrollHeight，这个分支会无条件命中，把最后一节永久点亮
      // 滚到底才让最后一节接管，且要求它确实已经进入视口——
      // 少了后一个条件，任何把页面推到底部的意外都会把高亮永久钉在末条
      const last = hs[hs.length - 1]
      const scrollable =
        document.documentElement.scrollHeight - window.innerHeight > 40
      if (scrollable
          && window.innerHeight + window.scrollY
             >= document.documentElement.scrollHeight - 4
          && last.getBoundingClientRect().top < window.innerHeight) {
        return setActive(last.id)
      }
      let cur = hs[0].id
      for (const h of hs) {
        if (h.getBoundingClientRect().top <= LINE) cur = h.id
        else break
      }
      setActive(cur)
    }
    const onScroll = () => { if (!raf) raf = requestAnimationFrame(compute) }

    compute()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    return () => {
      if (raf) cancelAnimationFrame(raf)
      window.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', onScroll)
    }
  }, [html])

  // 长文档的目录自身会滚动，高亮项要保证可见。
  //
  // **不能用 scrollIntoView**：它会滚动所有可滚动祖先，包括文档本身。
  // 那会形成正反馈——推动页面 → 触发滚动监听 → 重算 active → 再次推动，
  // 一路滚到底，然后 atEnd 分支把最后一条永久点亮。
  // 表现正是「点击能跳转，但目录一直高亮最后一条」。
  // 这里只改目录容器自己的 scrollTop，物理上不可能影响窗口。
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
      const el = bodyRef.current?.querySelector(`[id="${CSS.escape(href.slice(1))}"]`)
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
                    onClick={() => bodyRef.current
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
            <div className="md" ref={bodyRef} onClick={onBodyClick}
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
                       bodyRef.current?.querySelector(`[id="${CSS.escape(h.id)}"]`)
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
