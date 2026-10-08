import React, { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { BotAvatar } from 'bot-avatars'
import Markdown from 'react-markdown'
import { readChatStream, recentConversation } from '../lib/event-chat.js'

const suggestions = ['概括这件事', '哪些仍需核实？']
const idlePrompts = [
  '想问哪一条？我在。',
  '这件事的重点，要我捋捋吗？',
  '哪里还没核实？问我呀。',
  '一句话也行，问我看看。',
  '想看来源？我帮你找。',
  '还有疑问？我听着。',
]
let lastIdlePromptAt = 0
let idlePromptIndex = Math.floor(Math.random() * idlePrompts.length)

function Avatar({ state = 'default', size = 34 }) {
  return <BotAvatar type="clover" state={state} size={size} color="#8076cc" theme="light"
    shading="smooth" jumpEvery={0} turn={0.8} interactive />
}

function Icon({ name }) {
  if (name === 'new') return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 16.5V6.5A2.5 2.5 0 0 1 7.5 4h9A2.5 2.5 0 0 1 19 6.5v8a2.5 2.5 0 0 1-2.5 2.5H9l-4 3v-3.5Z" /><path d="M12 7.5v6m-3-3h6" /></svg>
  if (name === 'close') return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5l14 14M19 5 5 19" /></svg>
  if (name === 'stop') return <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6" /></svg>
}

export default function EventAssistant({ event }) {
  const [open, setOpen] = useState(false)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [webSearch, setWebSearch] = useState(false)
  const [webSources, setWebSources] = useState([])
  const [quickInput, setQuickInput] = useState('')
  const [launcherActive, setLauncherActive] = useState(false)
  const [pageVisible, setPageVisible] = useState(!document.hidden)
  const [hint, setHint] = useState('')
  const [hintCount, setHintCount] = useState(0)
  const pending = useRef(null), log = useRef(null), field = useRef(null)
  const launcher = useRef(null), launcherButton = useRef(null), quickField = useRef(null)
  const panel = useRef(null), follow = useRef(true)
  const lastActivityAt = useRef(Date.now())
  const sources = new Set([...(event.items || []).map(i => i.url), ...webSources])

  useEffect(() => () => pending.current?.abort(), [])
  useEffect(() => {
    const onVisibilityChange = () => setPageVisible(!document.hidden)
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => document.removeEventListener('visibilitychange', onVisibilityChange)
  }, [])
  useEffect(() => {
    const noteActivity = () => { lastActivityAt.current = Date.now() }
    const events = ['pointermove', 'pointerdown', 'keydown', 'wheel', 'touchmove', 'scroll']
    events.forEach(name => document.addEventListener(name, noteActivity, { passive: true, capture: true }))
    return () => events.forEach(name => document.removeEventListener(name, noteActivity, true))
  }, [])
  useEffect(() => {
    if (open || busy || quickInput.trim() || launcherActive || !pageVisible || hintCount >= 3 || hint) return
    const interval = hintCount === 0 ? 12000 : 60000
    let timer
    const showWhenIdle = () => {
      const wait = Math.max(lastActivityAt.current + 12000, lastIdlePromptAt + 45000) - Date.now()
      if (wait > 0) { timer = setTimeout(showWhenIdle, wait); return }
      lastIdlePromptAt = Date.now()
      setHint(idlePrompts[idlePromptIndex++ % idlePrompts.length])
      setHintCount(count => count + 1)
    }
    timer = setTimeout(showWhenIdle, Math.max(interval, lastIdlePromptAt + 45000 - Date.now()))
    return () => clearTimeout(timer)
  }, [open, busy, quickInput, launcherActive, pageVisible, hintCount, hint])
  useEffect(() => {
    if (!hint) return
    const timer = setTimeout(() => setHint(''), 4800)
    return () => clearTimeout(timer)
  }, [hint])
  useEffect(() => {
    if (open && follow.current && log.current) log.current.scrollTop = log.current.scrollHeight
  }, [messages, busy, error, open])
  useEffect(() => {
    if (!open) return
    const onKeyDown = e => {
      if (e.key === 'Escape') {
        setOpen(false)
        launcherButton.current?.focus({ preventScroll: true })
      }
    }
    const onPointerDown = e => {
      if (!panel.current?.contains(e.target) && !launcher.current?.contains(e.target)) setOpen(false)
    }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('pointerdown', onPointerDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('pointerdown', onPointerDown)
    }
  }, [open])

  function toggle() {
    setHint('')
    if (open) {
      setOpen(false)
    } else {
      setOpen(true)
      requestAnimationFrame(() => field.current?.focus({ preventScroll: true }))
    }
  }

  function submitQuick(e) {
    e.preventDefault()
    const question = quickInput.trim()
    if (!question || busy) { toggle(); return }
    setQuickInput('')
    setHint('')
    setOpen(true)
    ask(question)
    requestAnimationFrame(() => panel.current?.focus({ preventScroll: true }))
  }

  function syncLauncherActive() {
    requestAnimationFrame(() => setLauncherActive(launcher.current?.matches(':hover, :focus-within') || false))
  }

  async function ask(question, previous = messages, searchEnabled = webSearch) {
    question = question.trim()
    if (!question || question.length > 2000 || pending.current) return
    const history = recentConversation(previous, question)
    const stable = previous.filter(m => m.status === 'complete')
    const ctrl = new AbortController()
    pending.current = ctrl
    follow.current = true
    setMessages([...stable, { role: 'user', content: question, status: 'pending', webSearch: searchEnabled }, { role: 'assistant', content: '', status: 'pending' }])
    setInput(''); setError(''); setBusy(true)
    const update = (content, status) => setMessages([...stable,
      { role: 'user', content: question, status: status === 'complete' ? 'complete' : 'pending', webSearch: searchEnabled },
      { role: 'assistant', content, status }])
    let answer = ''
    const timer = setTimeout(() => ctrl.abort('timeout'), 95000)
    try {
      const r = await fetch(`/api/v1/events/${encodeURIComponent(event.id)}/chat`, {
        method: 'POST', signal: ctrl.signal, headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ messages: history, webSearch: searchEnabled }),
      })
      if (!r.ok) {
        const data = await r.json().catch(() => ({}))
        throw new Error(data.detail || '暂时无法回答，请重试')
      }
      await readChatStream(r.body, text => { answer = text; update(text, 'pending') },
        urls => setWebSources(previous => [...new Set([...previous, ...urls])]))
      update(answer, 'complete')
    } catch (e) {
      update(answer, 'incomplete')
      setError(ctrl.signal.aborted
        ? (ctrl.signal.reason === 'timeout' ? '等待超时' : '已停止生成')
        : e.message || '连接中断')
    } finally {
      clearTimeout(timer)
      if (pending.current === ctrl) { pending.current = null; setBusy(false) }
    }
  }
  function reset() {
    setMessages([]); setError(''); setInput(''); setWebSources([]); follow.current = true
    field.current?.focus({ preventScroll: true })
  }

  const retryQuestion = messages.at(-2)?.content
  return createPortal(<div className={`event-assistant-float ${open ? 'is-open' : ''}`}>
    {open && <section ref={panel} id="event-assistant-panel" className="assistant-panel" role="dialog" aria-label="事件助手" tabIndex={-1}>
      <header className="assistant-panel-head">
        <span className="assistant-panel-title">事件助手</span>
        <div className="assistant-panel-actions">
          {messages.length > 0 && <button type="button" onClick={reset} disabled={busy} title="新对话" aria-label="新对话"><Icon name="new" /></button>}
          <button type="button" onClick={() => { setOpen(false); launcherButton.current?.focus({ preventScroll: true }) }} title="关闭" aria-label="关闭事件助手"><Icon name="close" /></button>
        </div>
      </header>
      <div className={`assistant-log ${messages.length ? 'has-messages' : 'is-empty'}`} ref={log} role="region" aria-label="问答记录" tabIndex={0}
        onScroll={() => { const el = log.current; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 64 }}>
        {!messages.length && <div className="assistant-empty">
          <Avatar size={60} />
          <div className="assistant-suggestions">{suggestions.map(q =>
            <button type="button" key={q} onClick={() => ask(q)}>{q}<span aria-hidden="true">↗</span></button>)}</div>
        </div>}
        {messages.map((m, i) => <article key={i} className={`assistant-message ${m.role}`}>
          {m.role === 'assistant' && <div className="assistant-avatar" aria-hidden={i !== messages.length - 1}>
            <Avatar state={i === messages.length - 1 && busy ? 'working' : 'default'} size={34} />
          </div>}
          <div className={`assistant-answer ${m.status === 'pending' && m.role === 'assistant' && m.content ? 'streaming' : ''}`}>
            {m.content ? <Markdown skipHtml components={{
              a: ({ href, children }) => sources.has(href) && /^https?:\/\//i.test(href)
                ? <a href={href} target="_blank" rel="noopener noreferrer">{children} ↗</a> : <span>{children}</span>,
              img: () => null,
            }}>{m.content}</Markdown> : m.role === 'assistant' && busy
              ? <span className="assistant-dots" aria-label="正在回答"><i /><i /><i /></span> : null}
          </div>
        </article>)}
        {error && <div className="assistant-error" role="alert"><span>{error}</span>
          <button type="button" disabled={busy} onClick={() => ask(retryQuestion, messages, webSearch)}>重试</button></div>}
      </div>
      <form className="assistant-compose" onSubmit={e => { e.preventDefault(); ask(input) }}>
        <label className="sr-only" htmlFor="event-question">关于当前事件的问题</label>
        <textarea id="event-question" name="question" autoComplete="off" ref={field} value={input}
          onChange={e => setInput(e.target.value)} maxLength={2000} rows={2}
          placeholder="问问这件事…" disabled={busy}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) { e.preventDefault(); ask(input) } }} />
        <div className="assistant-compose-bottom">
          <button type="button" className={`fchip assistant-search-option ${webSearch ? 'on' : ''}`}
            aria-pressed={webSearch} disabled={busy} onClick={() => setWebSearch(value => !value)}>
            联网搜索
          </button>
          <div className="assistant-compose-actions">
            {busy ? <button type="button" aria-label="停止生成" title="停止生成" onClick={() => pending.current?.abort()}><Icon name="stop" /></button>
              : <button type="submit" aria-label="发送问题" title="发送问题" disabled={!input.trim()}><Icon name="send" /></button>}
          </div>
        </div>
      </form>
      <span className="sr-only" role="status" aria-live="polite">{busy ? '正在回答' : error || ''}</span>
    </section>}
    {hint && !open && !launcherActive && <button type="button" className="assistant-idle-hint"
      onClick={() => { setHint(''); quickField.current?.focus({ preventScroll: true }) }}>
      {hint}
    </button>}
    <form ref={launcher} className="assistant-launcher" onSubmit={submitQuick}
      onPointerEnter={() => { setLauncherActive(true); setHint('') }} onPointerLeave={syncLauncherActive}
      onFocusCapture={() => { setLauncherActive(true); setHint('') }} onBlurCapture={syncLauncherActive}>
      <input ref={quickField} className="assistant-launcher-input" type="text" aria-label="快速提问"
        autoComplete="off" maxLength={2000} value={quickInput} onChange={e => setQuickInput(e.target.value)}
        placeholder="问问这件事…" disabled={open || busy} />
      <button ref={launcherButton} type="submit" className={`assistant-launcher-action ${quickInput.trim() && !open && !busy ? 'has-draft' : ''}`}
        aria-label={quickInput.trim() && !open && !busy ? '发送快速提问' : open ? '关闭事件助手' : '打开事件助手'}
        aria-expanded={open} aria-haspopup="dialog" aria-controls="event-assistant-panel">
        <span className="assistant-launcher-bot" aria-hidden="true"><Avatar state={busy ? 'working' : 'default'} size={34} /></span>
        <span className="assistant-launcher-send" aria-hidden="true"><Icon name="send" /></span>
      </button>
    </form>
  </div>, document.body)
}
