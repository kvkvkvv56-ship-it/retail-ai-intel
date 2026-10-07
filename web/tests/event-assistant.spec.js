import { test, expect } from '@playwright/test'
const pagePath = '/events/EV-1c9c9501'
const answer = '报告覆盖 378 家企业。依据原文 [1](http://www.100ec.cn/detail--6659528.html)。'
const webAnswer = '补充材料见 [1](https://news.example.com/a)。'
function sse(content, sources = []) { return `${sources.length ? `data: ${JSON.stringify({ web_sources: sources })}\n\n` : ''}data: ${JSON.stringify({ choices: [{ delta: { content } }] })}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n` }
async function open(page) {
  await page.goto(pagePath)
  await page.getByRole('button', { name: '我知道了', exact: true }).click()
  await expect(page.getByRole('button', { name: '打开事件助手' })).toBeVisible()
  await expect(page.getByRole('dialog', { name: '事件助手' })).toHaveCount(0)
  await page.getByRole('button', { name: '打开事件助手' }).click()
  await expect(page.getByRole('dialog', { name: '事件助手' })).toBeVisible()
  await expect(page.getByRole('textbox', { name: '关于当前事件的问题' })).toBeVisible()
  await expect(page.getByRole('button', { name: '联网搜索' })).toHaveAttribute('aria-pressed', 'false')
}

test('floating launcher, question, follow-up context, reset and event isolation', async ({ page }) => {
  const requests = [], errors = []
  page.on('pageerror', e => errors.push(e.message))
  await page.route('**/api/v1/events/*/chat', async route => {
    const body = route.request().postDataJSON()
    requests.push(body)
    await route.fulfill({ contentType: 'text/event-stream', body: body.webSearch
      ? sse(webAnswer, ['https://news.example.com/a']) : sse(answer) })
  })
  await page.goto(pagePath)
  await page.getByRole('button', { name: '我知道了', exact: true }).click()
  const launcher = page.getByRole('button', { name: '打开事件助手' })
  const collapsed = await launcher.boundingBox()
  await launcher.hover()
  await expect.poll(async () => (await launcher.boundingBox()).width).toBeGreaterThan(collapsed.width + 100)
  await expect(page.getByText('问问这件事…', { exact: true })).toBeVisible()
  await page.screenshot({ path: '/private/tmp/event-assistant-hover.png', animations: 'disabled' })
  await launcher.click()
  await expect(page.getByRole('dialog', { name: '事件助手' })).toBeVisible()
  await page.getByRole('button', { name: '概括这件事', exact: true }).click()
  await expect(page.locator('.assistant-answer').last()).toContainText('378 家企业')
  await expect(page.locator('.assistant-answer a')).toHaveAttribute('href', 'http://www.100ec.cn/detail--6659528.html')
  await page.screenshot({ path: '/private/tmp/event-assistant-desktop.png', animations: 'disabled' })
  await page.locator('.assistant-panel').screenshot({ path: '/private/tmp/event-assistant-preview.png', animations: 'disabled' })
  await page.getByRole('button', { name: '关闭事件助手' }).last().click()
  await expect(page.getByRole('dialog', { name: '事件助手' })).toHaveCount(0)
  await page.getByRole('button', { name: '打开事件助手' }).click()
  await expect(page.locator('.assistant-message')).toHaveCount(2)
  await expect(page.getByRole('button', { name: '新对话' }).locator('svg path')).toHaveCount(2)
  await page.getByRole('button', { name: '联网搜索' }).click()
  await expect(page.getByRole('button', { name: '联网搜索' })).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('textbox', { name: '关于当前事件的问题' }).fill('这个样本有什么局限？')
  await page.getByRole('button', { name: '发送问题' }).click()
  await expect(page.locator('.assistant-message')).toHaveCount(4)
  await expect(page.locator('.assistant-answer a').last()).toHaveAttribute('href', 'https://news.example.com/a')
  expect(requests[1].messages.map(m => m.role)).toEqual(['user', 'assistant', 'user'])
  expect(requests[1].messages[1].content).toBe(answer)
  expect(requests[0].webSearch).toBe(false)
  expect(requests[1].webSearch).toBe(true)
  await page.screenshot({ path: '/private/tmp/event-assistant-search.png', animations: 'disabled' })
  await page.getByRole('button', { name: '新对话' }).click()
  await expect(page.locator('.assistant-message')).toHaveCount(0)
  await page.getByRole('button', { name: '概括这件事', exact: true }).click()
  await expect(page.locator('.assistant-message')).toHaveCount(2)
  await page.locator('.edge-item').first().click()
  await expect(page.locator('.assistant-message')).toHaveCount(0)
  expect(errors).toEqual([])
})

test('mobile floating layout, close, retry and stop', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  let mode = 'error'
  const requests = []
  await page.route('**/api/v1/events/*/chat', async route => {
    requests.push(route.request().postDataJSON())
    if (mode === 'error') return route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ detail: '问答服务暂不可用，请稍后再试' }) })
    if (mode === 'wait') { await new Promise(r => setTimeout(r, 2000)); return route.fulfill({ contentType: 'text/event-stream', body: sse(answer) }).catch(() => {}) }
    return route.fulfill({ contentType: 'text/event-stream', body: sse(answer) })
  })
  await open(page)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: '事件助手' })).toHaveCount(0)
  await expect(page.getByRole('textbox', { name: '关于当前事件的问题' })).toHaveCount(0)
  await page.getByRole('button', { name: '打开事件助手' }).click()
  await page.getByRole('button', { name: '联网搜索' }).click()
  await page.getByRole('button', { name: '概括这件事', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('问答服务暂不可用')
  mode = 'success'
  await page.getByRole('button', { name: '联网搜索' }).click()
  await page.getByRole('button', { name: '重试' }).click()
  await expect(page.locator('.assistant-answer').last()).toContainText('378 家企业')
  await expect(page.locator('.assistant-message')).toHaveCount(2)
  expect(requests.slice(0, 2).map(r => r.webSearch)).toEqual([true, false])
  await page.screenshot({ path: '/private/tmp/event-assistant-mobile.png', animations: 'disabled' })
  mode = 'wait'
  await page.getByRole('textbox').fill('继续解释')
  await page.getByRole('button', { name: '发送问题' }).click()
  await expect(page.getByRole('dialog', { name: '事件助手' }).getByRole('img', { name: /working/i })).toBeVisible()
  await page.getByRole('button', { name: '停止生成' }).click()
  await expect(page.getByRole('alert')).toContainText('已停止生成')
  await expect(page.getByRole('textbox')).toBeEnabled()
})

test('stream interruption and untrusted markdown stay safe', async ({ page }) => {
  await page.route('**/api/v1/events/*/chat', route => route.fulfill({ contentType: 'text/event-stream', body:
    `data: ${JSON.stringify({ choices: [{ delta: { content: '<script>window.bad=true</script> [bad](https://unknown.test) ![tracking](https://unknown.test/pixel)' } }] })}\n\n` }))
  await open(page)
  await page.getByRole('button', { name: '概括这件事', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('连接提前结束')
  await expect(page.locator('.assistant-answer a, .assistant-answer img, .assistant-answer script')).toHaveCount(0)
  expect(await page.evaluate(() => window.bad)).toBeUndefined()
})
