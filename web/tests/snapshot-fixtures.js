import { readFile } from 'node:fs/promises'

export async function useSnapshots(page) {
  await page.route('**/api/v1/**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.slice('/api/v1/'.length)
    if (request.method() !== 'GET') return route.fallback()
    if (!/^[a-zA-Z0-9-]+(?:\/[a-zA-Z0-9-]+)*$/.test(path)) return route.abort()
    try {
      const body = await readFile(new URL(`../public/api/v1/${path}.json`, import.meta.url), 'utf8')
      await route.fulfill({ contentType: 'application/json', body })
    } catch {
      await route.fulfill({ status: 404, json: { detail: 'No test snapshot' } })
    }
  })
}
