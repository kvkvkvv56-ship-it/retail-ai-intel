import React, { useEffect, useRef, useState } from 'react'

const CONF_CLS = {
  '官方确认·多源印证': 'conf-v', '多源已验证': 'conf-v',
  '官方一手': 'conf-o', '深度单源': 'conf-p', '单源待确认': 'conf-p',
}
const CONF_ICON = {
  '官方确认·多源印证': '◆', '多源已验证': '●', '官方一手': '◆',
  '深度单源': '◐', '单源待确认': '○',
}
const STAT_CLS = { 新增: 'new', 延续: 'cont', 静默: 'old', '旧闻/背景': 'old' }

export function Confidence({ value }) {
  if (!value) return null
  return (
    <span className={`conf ${CONF_CLS[value] || ''}`}>
      {CONF_ICON[value] || '·'} {value}
    </span>
  )
}

export function Status({ value }) {
  if (!value) return null
  return <span className={`pill ${STAT_CLS[value] || ''}`}>
    {value === '旧闻/背景' ? '背景' : value}
  </span>
}

/** 阶段刻度：四档全列，当前档加重——一眼看出落地进度而不是只给一个词 */
export function StageScale({ stages, value }) {
  const i = stages.indexOf(value)
  if (i < 0) return <span className="stage">阶段未判定</span>
  return (
    <span className="stage">
      {stages.map((s, k) => (
        <i key={s} className={k === i ? 'cur' : ''}>{k === i ? '● ' + s : s}</i>
      ))}
    </span>
  )
}

/** 时间口径（§6.3）：inferred 不参与窗口硬判定，须显式标出 */
export function TimeMark({ at, source }) {
  if (!at) return <span className="num">时间缺失</span>
  const p = { exact: '', parsed: '解析 ', inferred: '约 ' }[source] ?? ''
  return <span className="num" title={`时间来源：${source || '未知'}`}>{p}{at.slice(0, 10)}</span>
}

export const Empty = ({ children }) => <p className="empty">{children}</p>

/**
 * 窄屏折叠菜单。
 *
 * 顶部导航、周报往期、文档章节在手机上原本都是横向滚动条——横滚看不全，
 * 也不知道自己在第几项。统一改成「显示当前项 + 点开选择」：
 * 收起时只占一行，展开时是一份完整清单。
 *
 * 桌面端由 CSS 隐藏本组件、显示原来的常驻列表，两套结构互不干扰。
 */
export function Collapse({ label, current, children, className = '' }) {
  const [open, setOpen] = useState(false)
  const box = useRef(null)

  useEffect(() => {
    if (!open) return
    const away = (e) => { if (!box.current?.contains(e.target)) setOpen(false) }
    const esc = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', away)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('pointerdown', away)
      document.removeEventListener('keydown', esc)
    }
  }, [open])

  return (
    <div className={`collapse ${open ? 'open' : ''} ${className}`} ref={box}>
      <button className="collapse-head" onClick={() => setOpen(!open)}
              aria-expanded={open}>
        <span className="ch-k">{label}</span>
        <span className="ch-cur">{current}</span>
        <span className="ch-arrow" aria-hidden>▾</span>
      </button>
      {open && (
        <div className="collapse-body" onClick={() => setOpen(false)}>
          {children}
        </div>
      )}
    </div>
  )
}
