import React, { useEffect, useRef, useState } from 'react'

/**
 * 站点公告弹窗。
 *
 * 关闭后按 ID 记进 localStorage，同一条公告不再弹；换公告只需改 NOTICE_ID。
 * 隐私模式等场景读写会抛错 —— 吞掉即可，最坏情况是每次都弹。
 */
const NOTICE_ID = 'exa-quota-2026-09'
const KEY = `notice:${NOTICE_ID}`

const seen = () => { try { return localStorage.getItem(KEY) === '1' } catch { return false } }

export default function Notice() {
  const [open, setOpen] = useState(() => !seen())
  const btn = useRef(null)

  const close = () => {
    setOpen(false)
    try { localStorage.setItem(KEY, '1') } catch {}
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
          <div className="notice-kicker">公告 · NOTICE</div>
          <h2 id="notice-title">网络搜索服务暂时受限</h2>
          <p>
            由于 <b>Exa Search</b> 本月额度耗尽，网络搜索结果可能不全或暂停更新。
          </p>
          <p className="notice-next">
            后续将接入 <b>Agent Reach</b> 等服务，增强可用性。
          </p>
          <div className="notice-foot">
            <span className="notice-date">2026-09-27</span>
            <button ref={btn} className="notice-btn" onClick={close}>我知道了</button>
          </div>
        </div>
        <button className="notice-x" aria-label="关闭公告" onClick={close}>×</button>
      </div>
    </div>
  )
}
