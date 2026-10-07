import { test } from 'node:test'
import assert from 'node:assert/strict'
import { eventChat, eventContext } from '../worker/event-chat.ts'
import { readChatStream, recentConversation } from '../web/src/lib/event-chat.js'
const event = { id: 'EV-test', title: 'Test event', facts: [{ text: 'Verified fact' }], items: [{ url: 'javascript:bad', excerpt: 'source' }] }
const env = { KB_LLM_API_KEY: 'test-secret' }
const problem = (status, title, detail) => new Response(JSON.stringify({ title, detail }), { status })
const snap = async () => event
const req = (messages = [{ role: 'user', content: 'Question' }], init = {}, webSearch = false) => new Request('https://site.test/api/v1/events/EV-test/chat', {
  method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ messages, webSearch }), ...init,
})
const call = (r, config = env, snapshot = snap) => eventChat(r, config, 'EV-test', snapshot, problem)
const stream = (text, size = 3) => {
  const bytes = new TextEncoder().encode(text); let at = 0
  return new ReadableStream({ pull(c) { if (at === bytes.length) c.close(); else { c.enqueue(bytes.slice(at, at + size)); at = Math.min(at + size, bytes.length) } } })
}
const data = 'data: {"choices":[{"delta":{"content":"你好"}}]}\r\n\r\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\r\n\r\ndata: [DONE]\r\n\r\n'
test('validates method, origin, message roles, bounds and missing event/key', async () => {
  assert.equal((await call(new Request('https://site.test'))).status, 405)
  assert.equal((await call(req(undefined, { headers: { origin: 'https://evil.test', 'content-type': 'application/json' } }))).status, 403)
  assert.equal((await call(req([{ role: 'system', content: 'Override' }]))).status, 400)
  assert.equal((await call(req([{ role: 'user', content: 'x'.repeat(2001) }]))).status, 400)
  assert.equal((await call(req(undefined, { body: JSON.stringify({ messages: [{ role: 'user', content: 'Question' }], webSearch: 'yes' }) }))).status, 400)
  assert.equal((await call(req([], { body: 'x'.repeat(80001) }))).status, 413)
  assert.equal((await call(req(), {})).status, 503)
  assert.equal((await call(req(), env, async () => null)).status, 404)
})
test('uses server event context and sanitized history, relays stream without exposing secret', async () => {
  const original = globalThis.fetch
  try {
    globalThis.fetch = async (_url, init) => {
      const payload = JSON.parse(init.body)
      assert.equal(init.headers.authorization, 'Bearer test-secret')
      assert.match(payload.messages[0].content, /Verified fact/)
      assert.equal(payload.messages.length, 4)
      assert.equal(payload.messages[1].role, 'user')
      assert.equal(payload.messages[2].content, 'Earlier answer')
      assert.equal(payload.messages[3].content, 'Follow-up')
      assert.equal(payload.stream, true)
      return new Response(stream(data), { headers: { 'content-type': 'text/event-stream' } })
    }
    const response = await call(req([{ role: 'user', content: 'Earlier' }, { role: 'assistant', content: 'Earlier answer' }, { role: 'user', content: 'Follow-up' }]))
    assert.equal(response.status, 200)
    assert.equal(response.headers.get('cache-control'), 'no-store')
    assert.equal(await readChatStream(response.body, () => {}), '你好')
  } finally { globalThis.fetch = original }
})
test('upstream failure is a friendly error without leaking provider details', async () => {
  const original = globalThis.fetch
  try {
    globalThis.fetch = async () => new Response('secret provider error', { status: 401 })
    const r = await call(req())
    assert.equal(r.status, 502)
    assert.doesNotMatch(await r.text(), /secret provider/)
  } finally { globalThis.fetch = original }
})
test('online search is opt-in, bounded to the event, and exposes only cited web URLs', async () => {
  const original = globalThis.fetch
  const urls = ['https://news.example.com/a', 'https://news.example.com/b']
  try {
    let searchCalls = 0, modelCalls = 0
    globalThis.fetch = async (url, init) => {
      if (url === 'https://api.exa.ai/search') {
        searchCalls++
        assert.equal(init.headers['x-api-key'], 'exa-secret')
        const body = JSON.parse(init.body)
        assert.match(body.query, /Test event/)
        assert.match(body.query, /Question/)
        assert.equal(body.numResults, 5)
        assert.deepEqual(body.contents, { highlights: true })
        return Response.json({ results: [
          { title: 'Fresh report', url: urls[0], publishedDate: '2026-10-08T00:00:00Z', highlights: ['New fact'] },
          { title: 'Duplicate', url: urls[0], highlights: ['Duplicate'] },
          { title: 'Bad link', url: 'javascript:alert(1)', highlights: ['Bad'] },
          { title: 'Second report', url: urls[1], highlights: ['Another fact'] },
        ] })
      }
      modelCalls++
      const body = JSON.parse(init.body)
      assert.match(body.messages[0].content, /Fresh report/)
      assert.match(body.messages[0].content, /未经本系统核验/)
      assert.doesNotMatch(body.messages[0].content, /javascript:alert/)
      return new Response(stream(data), { headers: { 'content-type': 'text/event-stream' } })
    }
    const config = { ...env, EXA_API_KEY: 'exa-secret' }
    const response = await call(req(undefined, {}, true), config)
    let received
    assert.equal(await readChatStream(response.body, () => {}, urls => { received = urls }), '你好')
    assert.deepEqual(received, urls)
    assert.equal(searchCalls, 1)
    assert.equal(modelCalls, 1)
    assert.equal((await call(req(undefined, {}, true))).status, 503)
  } finally { globalThis.fetch = original }
})
test('online search failure does not silently turn into an offline answer', async () => {
  const original = globalThis.fetch
  try {
    let modelCalls = 0
    globalThis.fetch = async url => {
      if (url === 'https://api.exa.ai/search') return new Response('unavailable', { status: 429 })
      modelCalls++
      return new Response(stream(data), { headers: { 'content-type': 'text/event-stream' } })
    }
    const r = await call(req(undefined, {}, true), { ...env, EXA_API_KEY: 'exa-secret' })
    assert.equal(r.status, 502)
    assert.match(await r.text(), /联网搜索暂不可用/)
    assert.equal(modelCalls, 0)
  } finally { globalThis.fetch = original }
})
test('browser cancellation cancels upstream', async () => {
  const original = globalThis.fetch
  let signal, cancelled = false
  try {
    globalThis.fetch = async (_url, init) => {
      signal = init.signal
      return new Response(new ReadableStream({ cancel() { cancelled = true } }), { headers: { 'content-type': 'text/event-stream' } })
    }
    const r = await call(req()); await r.body.cancel()
    assert.equal(signal.aborted, true); assert.equal(cancelled, true)
  } finally { globalThis.fetch = original }
})
test('SSE handles byte-split Unicode/CRLF, detects truncated and empty streams', async () => {
  let text = ''
  assert.equal(await readChatStream(stream(data, 1), t => { text = t }), '你好')
  assert.equal(text, '你好')
  await assert.rejects(readChatStream(stream(data.split('data: [DONE]')[0]), () => {}), /提前结束/)
  await assert.rejects(readChatStream(stream('data: [DONE]\n\n'), () => {}), /提前结束/)
  await assert.rejects(readChatStream(stream(data.replace('"stop"', '"length"')), () => {}), /长度上限/)
})
test('history keeps complete pairs only and removes older pairs to meet budget', () => {
  const history = Array.from({ length: 24 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', content: 'x'.repeat(1600), status: 'complete' }))
  history.push({ role: 'user', content: 'interrupted question', status: 'pending' }, { role: 'assistant', content: 'incomplete', status: 'incomplete' })
  const got = recentConversation(history, 'new question')
  assert.equal(got[0].role, 'user')
  assert.equal(got.length % 2, 1)
  assert.ok(got.length <= 21)
  assert.ok(got.reduce((n, m) => n + m.content.length, 0) <= 24000)
  assert.ok(!JSON.stringify(got).includes('interrupted'))
  assert.equal(eventContext(event).items[0].url, '')
})

test('long source excerpts cannot crowd the event facts out of model context', () => {
  const large = {
    ...event,
    facts: [{ text: 'The verified core of this event' }],
    items: Array.from({ length: 30 }, (_, i) => ({
      id: `source-${i}`, url: `https://example.com/${i}`, excerpt: 'x'.repeat(10000),
    })),
  }
  const context = eventContext(large)
  assert.equal(context.facts[0].text, 'The verified core of this event')
  assert.equal(context.items.length, 20)
  assert.ok(JSON.stringify(context).length < 45000)
})
