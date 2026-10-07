/** Read DeepSeek SSE without losing frames split across network chunks. */
export async function readChatStream(body, onToken, onSources = () => {}) {
  if (!body) throw new Error('未收到回答，请重试')
  const reader = body.getReader(), decoder = new TextDecoder()
  let buffer = '', ended = false, content = '', reason = null
  const frame = (block) => {
    const data = block.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trim()).join('\n')
    if (!data) return
    if (data === '[DONE]') { ended = true; return }
    const j = JSON.parse(data)
    if (j.error) throw new Error('生成失败，请稍后重试')
    if (Array.isArray(j.web_sources)) {
      onSources(j.web_sources.filter(url => typeof url === 'string' && /^https?:\/\//i.test(url)))
      return
    }
    const choice = j.choices?.[0]
    if (choice?.finish_reason) reason = choice.finish_reason
    const token = choice?.delta?.content
    if (typeof token === 'string') { content += token; onToken(content) }
  }
  try {
    while (!ended) {
      const { done, value } = await reader.read()
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
      buffer = buffer.replace(/\r\n/g, '\n')
      let cut
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        frame(buffer.slice(0, cut)); buffer = buffer.slice(cut + 2)
      }
      if (done) { if (buffer.trim()) frame(buffer); break }
    }
    if (!ended || !content.trim()) throw new Error('连接提前结束，请重试本次问题')
    if (reason === 'length') throw new Error('回答达到长度上限，请重试并缩小问题范围')
    if (reason && reason !== 'stop') throw new Error('回答未完成，请换一种方式提问')
    return content
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
}

/** Keep complete recent turns within the server's bounded context window. */
export function recentConversation(messages, question) {
  const complete = messages.filter(m => m.status === 'complete')
  let history = complete.slice(-20)
  while (history.length && history.reduce((n, m) => n + m.content.length, question.length) > 24000) history = history.slice(2)
  return [...history.map(({ role, content }) => ({ role, content })), { role: 'user', content: question }]
}
