import { chromium } from 'playwright'
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'

// 用本地 dist 起个静态服，API 反代到线上
const DIST = path.resolve('dist')
const MIME = { '.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.json':'application/json', '.svg':'image/svg+xml', '.ttf':'font/ttf', '.woff2':'font/woff2', '.png':'image/png' }
const srv = http.createServer(async (req, res) => {
  const u = new URL(req.url, 'http://x')
  if (u.pathname.startsWith('/api/')) {
    const r = await fetch('https://ai.kvkvkvv.site' + u.pathname + u.search)
    res.writeHead(r.status, { 'content-type': r.headers.get('content-type') || 'application/json' })
    return res.end(Buffer.from(await r.arrayBuffer()))
  }
  let f = path.join(DIST, u.pathname)
  if (!fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(DIST, 'index.html')
  res.writeHead(200, { 'content-type': MIME[path.extname(f)] || 'application/octet-stream' })
  fs.createReadStream(f).pipe(res)
})
await new Promise(r => srv.listen(4173, r))

const b = await chromium.launch()
const p = await b.newPage({ viewport: { width: 1440, height: 900 } })
await p.goto('http://localhost:4173/docs/methodology', { waitUntil: 'networkidle' })
await p.waitForSelector('.docs-toc a'); await p.waitForTimeout(1200)

const read = () => p.evaluate(() => {
  const links = [...document.querySelectorAll('.docs-toc a')]
  const on = document.querySelector('.docs-toc a.on')
  const hs = [...document.querySelectorAll('.md h2[id], .md h3[id]')]
  let want = 0
  for (let i = 0; i < hs.length; i++) { if (hs[i].getBoundingClientRect().top <= 120) want = i; else break }
  return { y: Math.round(scrollY), got: on ? links.indexOf(on) : -1, want, txt: on?.textContent.trim().slice(0,18) }
})

let bad = 0
console.log('滚动轨迹:')
for (const y of [0, 500, 1200, 2200, 3400, 4800, 6200, 99999]) {
  await p.evaluate(yy => window.scrollTo(0, yy), y); await p.waitForTimeout(500)
  const r = await read(); const ok = r.got === r.want; if (!ok) bad++
  console.log(`  ${ok?'✓':'✗'} y=${String(r.y).padStart(5)}  高亮=${String(r.got).padStart(2)} 应为=${String(r.want).padStart(2)}  ${r.txt}`)
}
console.log('\n点击目录项:')
for (const i of [4, 9, 14, 1, 16]) {
  await p.evaluate(k => document.querySelectorAll('.docs-toc a')[k].click(), i)
  await p.waitForTimeout(1500)
  const r = await read(); const ok = r.got === i; if (!ok) bad++
  console.log(`  ${ok?'✓':'✗'} 点第${String(i).padStart(2)}条 → 高亮第${String(r.got).padStart(2)}条  ${r.txt}`)
}
// 周报侧栏
await p.goto('http://localhost:4173/report', { waitUntil: 'networkidle' })
await p.waitForSelector('.side-item'); await p.waitForTimeout(800)
const rep = await p.evaluate(() => ({
  items: document.querySelectorAll('.side-item').length,
  expandBtn: document.querySelectorAll('.side-expand').length,
  more: document.querySelector('.side-more')?.textContent.trim(),
}))
console.log('\n周报侧栏:', JSON.stringify(rep))
console.log(bad ? `\n❌ 不一致 ${bad} 处` : '\n✅ 目录高亮全部正确')
await b.close(); srv.close()
