import React, { useEffect, useRef, useState } from 'react'

/**
 * 站点公告弹窗。
 *
 * 每次打开网页都弹；勾选「今日内不再弹出」后关闭，按本地日期记进 localStorage，
 * 当天不再弹、次日照常弹，直到公告下线（从 App 里移除 <Notice />）。
 * 换公告只需改 NOTICE_ID。隐私模式等场景读写会抛错 —— 吞掉即可，最坏情况是每次都弹。
 */
const NOTICE_ID = 'exa-restored-2026-10-07'
// 与旧版「关过就永不再弹」的 `notice:${NOTICE_ID}` 键区分开，已关过的访客也会重新看到
const KEY = `notice:${NOTICE_ID}:snoozed-on`

const today = () => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

const snoozedToday = () => { try { return localStorage.getItem(KEY) === today() } catch { return false } }

export default function Notice() {
  const [open, setOpen] = useState(() => !snoozedToday())
  const btn = useRef(null)
  const box = useRef(null)   // 用 ref 读勾选状态：Esc 监听里的 close 不必随勾选重建

  const close = () => {
    setOpen(false)
    try {
      if (box.current?.checked) localStorage.setItem(KEY, today())
      else localStorage.removeItem(KEY)
    } catch {}
  }

  useEffect(() => {
    if (!open) return
    btn.current?.focus()
    const onKey = (e) => { if (e.key === 'Escape') close() }
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [open])

  if (!open) return null

  return (
    <div className="notice-backdrop" onClick={close}>
      <div className="notice" role="dialog" aria-modal="true" aria-labelledby="notice-title"
           onClick={(e) => e.stopPropagation()}>
        <div className="notice-art">
          <img src="/notice/search-restored.svg" alt="" />
        </div>
        <div className="notice-body">
          <div className="notice-kicker">公告 · NOTICE · 2026-10-07</div>
          <h2 id="notice-title">网络搜索服务已恢复</h2>
          <p>
            <b>网络搜索已恢复可用</b>，现在可以继续检索公开信息，为简报补充网络来源。
          </p>
          <p className="notice-next">
            感谢你的耐心等待。你可以继续探索行业动态，生成所需的洞察简报。
          </p>
          <div className="notice-foot">
            <label className="notice-snooze">
              <input ref={box} type="checkbox" />
              今日内不再弹出
            </label>
            <button ref={btn} className="notice-btn" onClick={close}>我知道了</button>
          </div>
        </div>
        <button className="notice-x" aria-label="关闭公告" onClick={close}>×</button>
      </div>
    </div>
  )
}
