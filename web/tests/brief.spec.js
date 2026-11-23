import { test, expect } from '@playwright/test'
import { useSnapshots } from './snapshot-fixtures.js'

const report = [
  '## 一、本期主线',
  '',
  '| 位置 | 载体 | EV |',
  '| --- | --- | --- |',
  '| 消费者入口 | 夸克 AI 眼镜 | EV-1c9c9501 |',
  '',
  '判断：AI 交易链路推进。',
  '',
  '---',
  '',
  '## 二、跨境进展',
  '',
  '* * *',
  '',
  '原始标记 --- 可保留在正文中。',
].join('\n')

test('the old site opens the brief without the retired notice or decorative dividers', async ({ page }) => {
  await useSnapshots(page)
  await page.route('**/api/v1/briefs/generate', route => route.fulfill({
    contentType: 'text/event-stream',
    body: `event: token\ndata: ${JSON.stringify({ t: report })}\n\nevent: done\ndata: {"elapsed_ms":50}\n\n`,
  }))
  await page.goto('/brief')
  await expect(page.getByRole('dialog', { name: '网络搜索服务已恢复' })).toHaveCount(0)
  await page.getByRole('button', { name: '生成简报' }).click()

  const brief = page.locator('.brief-body')
  const heading = brief.getByRole('heading', { name: '二、跨境进展' })
  await expect(heading).toBeVisible()
  await expect(heading).toHaveCSS('border-top-width', '0px')
  await expect(brief.getByRole('separator')).toHaveCount(0)
  await expect(brief.locator('p').filter({ hasText: /^(?:---|\* \* \*)$/ })).toHaveCount(0)
  await expect(brief.getByText('原始标记 --- 可保留在正文中。')).toBeVisible()
  await expect(brief.getByRole('columnheader')).toHaveCount(3)
  await expect(page.getByRole('button', { name: '生成简报' })).toBeEnabled()
  const citation = brief.getByRole('button', { name: 'EV-1c9c9501' })
  await citation.focus()
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/\/events\/EV-1c9c9501$/)
})
