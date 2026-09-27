import React, { useEffect, useRef, useState } from 'react'

/**
 * 站点公告弹窗。
 *
 * 每次打开网页都弹；勾选「今日内不再弹出」后关闭，按本地日期记进 localStorage，
 * 当天不再弹、次日照常弹，直到公告下线（从 App 里移除 <Notice />）。
 * 换公告只需改 NOTICE_ID。隐私模式等场景读写会抛错 —— 吞掉即可，最坏情况是每次都弹。
 */
const NOTICE_ID = 'exa-quota-2026-09'
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
          <img src="/notice/retro-search.svg" alt="" />
        </div>
        <div className="notice-body">
          <div className="notice-kicker">公告 · NOTICE · 2026-09-27</div>
          <h2 id="notice-title">网络搜索服务暂时受限</h2>
          <p>
            由于 <b>Exa Search</b> 本月额度耗尽，网络搜索结果可能不全或暂停更新。
          </p>
          <p className="notice-next">
            后续将接入 <b>Agent Reach</b> 等服务，增强可用性。
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
