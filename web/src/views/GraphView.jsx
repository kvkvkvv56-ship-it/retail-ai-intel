import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import { Empty, Loading } from '../components/Bits.jsx'

/**
 * 知识图谱 —— 事件为节点，关系边为连线，力导向布局。
 *
 * ── 为什么边按「关系」着色而不按「公司」 ──────────────────────────
 * 直觉做法是一家公司一个颜色，但这里有 9 家，而节点图里任意两个颜色都可能
 * 挨在一起。跑了分类调色板的校验（validate_palette.js 全对模式）：标准
 * 八色板在全对模式下只有前三个槽位能同时过 CVD 分离度与正常视觉分离度，
 * 第九个颜色更是只能靠生成——那是明确的反模式。
 *
 * 所以公司身份改由**空间聚类 + 簇心直接标注**承担：规则边本来就把同一家
 * 公司的事件按时间串成一条链，力导向会自然把它们收在一起，簇心打一个公司名
 * 就够了。腾出来的颜色通道给真正需要区分的东西——关系的种类。
 *
 * 三色取自校验通过的槽位（全对模式最差 CVD ΔE 15.3、正常视觉 ΔE 20.8，
 * 均 PASS）。amber 对白底对比度 2.17 低于 3:1，按 relief 规则以常驻图例文字
 * 与事件库列表作为替代通道；另有两重次级编码：方向性关系带箭头，矛盾关系
 * 走虚线——不依赖颜色也能分辨。
 */
const REL = {
  follows: { c: '#2a78d6', label: '后续进展', dir: true },
  causes: { c: '#2a78d6', label: '因果关联', dir: true },
  corroborates: { c: '#eda100', label: '相互印证', dir: false },
  contradicts: { c: '#e34948', label: '存在矛盾', dir: false, dash: [5, 4] },
  same_actor_track: { c: '#C2C2C2', label: '同主体动作', dir: false, rule: true },
}
const FALLBACK_REL = { c: '#8A8A8A', label: '关联', dir: false }
const relOf = (r) => REL[r] || FALLBACK_REL

/* 图例只列实际出现的类别；follows 与 causes 共用蓝色，合并成一行说明 */
const LEGEND = [
  { keys: ['follows', 'causes'], c: '#2a78d6', label: '后续进展 · 因果关联', arrow: true },
  { keys: ['corroborates'], c: '#eda100', label: '相互印证' },
  { keys: ['contradicts'], c: '#e34948', label: '存在矛盾', dash: true },
  { keys: ['same_actor_track'], c: '#C2C2C2', label: '同主体动作（规则边）' },
]

/* ─────────────────────────────── 力导向模拟 ───────────────────────────────
 *
 * 不引第三方图库：项目前端只有 React，为一个视图加 d3-force + 渲染层会把
 * 依赖翻倍，而这里需要的三个力一共二十来行。
 *
 * 斥力是 O(n²)。289 节点 = 4.2 万对/帧，JS 里一两毫秒，够用；真正的保护是
 * **总迭代数固定**：布局分帧跑完 ~420 次迭代就停，之后只在交互时重绘，
 * 不做常驻模拟。这样即使事件涨到几千，代价也只是收敛慢几秒，不会持续烧 CPU。
 * 另加 300px 截断——超出这个距离的斥力对布局没有可见影响，砍掉省一半计算。
 */
const SIM = {
  charge: 2600,     // 斥力系数，按距离平方衰减
  cutoff: 420 * 420,
  linkDist: 46,     // 边的理想长度
  linkStr: 0.22,
  anchor: 0.016,    // 朝本公司锚点的引力
  sep: 7,           // 两节点之间至少留出的像素间隙
  damp: 0.82,
  decay: 0.9885,
  ticks: 460,
  perFrame: 14,
}

/**
 * 按公司锚点成岛。
 *
 * 第一版用的是「全局向心力 + 会衰减的同公司聚拢力」，跑出来是一团均匀的网：
 * 289 个点铺满画布，9 个公司标签散在中间，看不出任何结构——因为聚拢力衰减到
 * 零之后，剩下的斥力与向心力本来就只会得到一个圆盘。
 *
 * 改成每家公司一个固定锚点、常驻引力。这不是给图强加结构：规则边的定义就是
 * 「同公司、时间相邻」，459 条边里 459 条都在公司内部，公司分组**就是**这张图
 * 的边结构本身。锚点只是把这件既成事实摆到看得见的位置上。
 * 真正跨公司的语义边则会拉着两座岛互相靠拢——那才是要看的东西。
 *
 * 锚点按事件数加权分配角度：阿里 67 条和快手 6 条如果均分圆周，前者会挤成
 * 一坨、后者空一大片。
 */
function buildLayout(nodes, edges, companies, degOf) {
  const n = nodes.length
  const idx = new Map(nodes.map((d, i) => [d.id, i]))
  const x = new Float64Array(n), y = new Float64Array(n)
  const vx = new Float64Array(n), vy = new Float64Array(n)

  const count = new Map()
  for (const d of nodes) count.set(d.c, (count.get(d.c) || 0) + 1)
  const weight = companies.map((c) => Math.sqrt(count.get(c) || 1))
  const totalW = weight.reduce((a, b) => a + b, 0) || 1
  // 半径由「所有簇的周长需求」倒推，簇之间才不会互相挤压
  // 一个 n 点的簇大致要占 11√n 的半径；把所有簇的直径摊到一个圆周上，
  // 再留 1.5 倍余量，岛之间才不会挤在一起（早先系数取 42，实测阿里/行业/
  // ai_vendor 三座大岛直接糊成一片）
  const R = companies.length < 2 ? 0 : Math.max(240, (66 * totalW) / (2 * Math.PI))

  const slot = new Map(companies.map((c, i) => [c, i]))
  const cx = new Float64Array(companies.length), cy = new Float64Array(companies.length)
  let acc = 0
  companies.forEach((c, i) => {
    const a = ((acc + weight[i] / 2) / totalW) * Math.PI * 2
    acc += weight[i]
    cx[i] = Math.cos(a) * R
    cy[i] = Math.sin(a) * R
  })

  const home = new Int32Array(n)
  nodes.forEach((d, i) => {
    const s = slot.has(d.c) ? slot.get(d.c) : 0
    home[i] = s
    const a = Math.random() * Math.PI * 2
    const r = 8 + Math.random() * 30
    x[i] = cx[s] + Math.cos(a) * r
    y[i] = cy[s] + Math.sin(a) * r
  })

  const links = []
  for (const e of edges) {
    const a = idx.get(e.f), b = idx.get(e.t)
    if (a != null && b != null && a !== b) links.push([a, b])
  }

  const rad = new Float64Array(n)
  nodes.forEach((d, i) => { rad[i] = radius(degOf(d.id)) })

  return { n, x, y, vx, vy, links, home, cx, cy, rad, idx, companies, alpha: 1 }
}

function tick(st) {
  const { n, x, y, vx, vy, links, home, cx, cy, rad } = st
  const a = st.alpha

  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      let dx = x[j] - x[i], dy = y[j] - y[i]
      let d2 = dx * dx + dy * dy
      if (d2 > SIM.cutoff) continue
      if (d2 < 0.01) {                    // 完全重合时给一个随机方向推开
        dx = Math.random() - 0.5
        dy = Math.random() - 0.5
        d2 = dx * dx + dy * dy + 0.01
      }
      const d = Math.sqrt(d2)
      const f = (SIM.charge * a) / d2
      const fx = (dx / d) * f, fy = (dy / d) * f
      vx[i] -= fx; vy[i] -= fy
      vx[j] += fx; vy[j] += fy

      // 短程分离：**直接改位置、且不乘 alpha**。
      // 斥力是随 alpha 熄火的，只靠它的话最后一帧节点仍会互相压住；
      // 这一项是硬约束，保证收敛后任意两个圆之间都留得下缝。
      const sep = rad[i] + rad[j] + SIM.sep
      if (d < sep) {
        const m = ((sep - d) / d) * 0.5
        x[i] -= dx * m; y[i] -= dy * m
        x[j] += dx * m; y[j] += dy * m
      }
    }
  }

  for (const [i, j] of links) {
    const dx = x[j] - x[i], dy = y[j] - y[i]
    const d = Math.sqrt(dx * dx + dy * dy) || 1
    const f = (d - SIM.linkDist) * SIM.linkStr * a
    const fx = (dx / d) * f, fy = (dy / d) * f
    vx[i] += fx; vy[i] += fy
    vx[j] -= fx; vy[j] -= fy
  }

  // 锚点引力**必须和斥力、弹簧一样乘 alpha**。
  // 第一版没乘：alpha 衰减到 0.005 之后斥力和弹簧都熄了，只剩锚点还在拉，
  // 于是每家公司在最后一百帧里被压成一颗实心球，图上看不到任何链式结构。
  // 三个力同步缩放时，平衡点与 alpha 无关，alpha 只决定收敛快慢。
  const af = SIM.anchor * a
  for (let i = 0; i < n; i++) {
    const h = home[i]
    vx[i] += (cx[h] - x[i]) * af
    vy[i] += (cy[h] - y[i]) * af
    vx[i] *= SIM.damp; vy[i] *= SIM.damp
    x[i] += vx[i]; y[i] += vy[i]
  }
  st.alpha *= SIM.decay
}

/** 收敛后把整张图缩放平移到画布里。不做的话 289 个点会溢出画布边缘。 */
function fitView(st, W, H) {
  if (!st.n) return { x: 0, y: 0, k: 1 }
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity
  for (let i = 0; i < st.n; i++) {
    if (st.x[i] < x0) x0 = st.x[i]
    if (st.x[i] > x1) x1 = st.x[i]
    if (st.y[i] < y0) y0 = st.y[i]
    if (st.y[i] > y1) y1 = st.y[i]
  }
  const pad = 46
  const k = Math.min(2.4, Math.max(0.22,
    Math.min((W - pad * 2) / Math.max(1, x1 - x0), (H - pad * 2) / Math.max(1, y1 - y0))))
  return { x: (x0 + x1) / 2, y: (y0 + y1) / 2, k }
}

/* ────────────────────────────────── 视图 ────────────────────────────────── */
export default function GraphView({ meta, nav }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [semOnly, setSemOnly] = useState(false)
  const [comp, setComp] = useState(new Set())
  const [sel, setSel] = useState(null)
  const [hover, setHover] = useState(null)
  const [progress, setProgress] = useState(0)

  const canvas = useRef(null)
  const wrap = useRef(null)
  const st = useRef(null)                       // 模拟状态
  const cam = useRef({ x: 0, y: 0, k: 1 })
  const drag = useRef(null)
  const raf = useRef(0)

  useEffect(() => { api('graph').then(setData).catch((e) => setErr(String(e))) }, [])

  const compName = useMemo(
    () => Object.fromEntries((meta?.companies || []).map((c) => [c.id, c.name])), [meta])

  /* ── 可见子图。筛选在这里一次算完，模拟只认它的结果 ── */
  const view = useMemo(() => {
    if (!data) return null
    const semantic = (e) => e.by !== 'rule'
    let edges = data.edges.filter((e) => (semOnly ? semantic(e) : true))

    let nodes = data.nodes
    if (comp.size) nodes = nodes.filter((n) => comp.has(n.c))
    // 只看语义关联时把孤立点也去掉：285 个没有语义边的节点铺在屏幕上，
    // 会把仅有的那几条真关系彻底淹没
    if (semOnly) {
      const touched = new Set()
      edges.forEach((e) => { touched.add(e.f); touched.add(e.t) })
      nodes = nodes.filter((n) => touched.has(n.id))
    }
    const ids = new Set(nodes.map((n) => n.id))
    edges = edges.filter((e) => ids.has(e.f) && ids.has(e.t))

    const adj = new Map(nodes.map((n) => [n.id, []]))
    for (const e of edges) {
      adj.get(e.f)?.push({ other: e.t, rel: e.r, dir: 'out', by: e.by, basis: e.b })
      adj.get(e.t)?.push({ other: e.f, rel: e.r, dir: 'in', by: e.by, basis: e.b })
    }
    const deg = new Map(nodes.map((n) => [n.id, adj.get(n.id).length]))
    return { nodes, edges, adj, deg, byId: new Map(nodes.map((n) => [n.id, n])) }
  }, [data, semOnly, comp])

  const companies = useMemo(
    () => [...new Set((view?.nodes || []).map((n) => n.c))], [view])

  /* 焦点跟着子图走：被筛掉的节点不能继续留在侧栏里 */
  useEffect(() => {
    if (sel && view && !view.byId.has(sel)) setSel(null)
    if (hover && view && !view.byId.has(hover)) setHover(null)
  }, [view, sel, hover])

  /* ── 布局：分帧跑固定次数的迭代，跑完即停 ── */
  useEffect(() => {
    if (!view || !view.nodes.length) return
    st.current = buildLayout(view.nodes, view.edges, companies, (id) => view.deg.get(id))
    cam.current = { x: 0, y: 0, k: 1 }
    let done = 0
    setProgress(0)
    const step = () => {
      for (let i = 0; i < SIM.perFrame && done < SIM.ticks; i++, done++) tick(st.current)
      // 每帧都重新取景：布局在收缩，镜头跟着收，看起来像图自己长出来
      const cv = canvas.current
      if (cv) cam.current = fitView(st.current, cv.clientWidth, cv.clientHeight)
      setProgress(done / SIM.ticks)
      draw()
      if (done < SIM.ticks) raf.current = requestAnimationFrame(step)
    }
    cancelAnimationFrame(raf.current)
    raf.current = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, companies])

  /* ── 绘制 ── */
  const draw = useCallback(() => {
    const cv = canvas.current, s = st.current
    if (!cv || !s || !view) return
    const dpr = window.devicePixelRatio || 1
    const W = cv.clientWidth, H = cv.clientHeight
    if (cv.width !== W * dpr || cv.height !== H * dpr) {
      cv.width = W * dpr; cv.height = H * dpr
    }
    const g = cv.getContext('2d')
    g.setTransform(dpr, 0, 0, dpr, 0, 0)
    g.clearRect(0, 0, W, H)

    const { x, y, idx } = s
    const c = cam.current
    const SX = (i) => (x[i] - c.x) * c.k + W / 2
    const SY = (i) => (y[i] - c.y) * c.k + H / 2

    // 高亮集合：悬停或选中的节点 + 它的直接邻居。
    //
    // 必须先确认焦点节点还在当前子图里：切「只看语义关联」或改公司筛选时，
    // hover/sel 仍指着一个已经被筛掉的 id，lit 里只有它自己、而它又不在图上，
    // 于是 dim 为真、所有边都被判为「不相邻」而整张图空白——实测切过去就是
    // 一片空画布加几个灰点，看起来像功能坏了。
    const raw = hover || sel
    const focusId = raw && view.byId.has(raw) ? raw : null
    const lit = new Set()
    if (focusId) {
      lit.add(focusId)
      for (const l of view.adj.get(focusId) || []) lit.add(l.other)
    }
    const dim = lit.size > 0

    // ── 公司岛的水印标注（垫在最底层）──
    // 公司身份的主通道就是这个：位置 + 直接标注，而不是 9 个分类色。
    // 画在锚点上而不是节点质心上——质心会被跨公司的语义边拽偏，
    // 标签跟着飘，反而说不清哪块是哪家。
    if (!dim && s.companies) {
      g.textAlign = 'center'
      for (let i = 0; i < s.companies.length; i++) {
        const key = s.companies[i]
        const n = view.nodes.reduce((a, d) => a + (d.c === key ? 1 : 0), 0)
        if (!n) continue
        const ax = (s.cx[i] - c.x) * c.k + W / 2
        // 标签往岛的上沿挪：压在锚点上等于压在最密的地方，字被节点盖得认不出。
        // 岛半径按「n 个点各占约 11px」估，与布局里算锚点圆周用的是同一个估计。
        const ay = (s.cy[i] - c.y) * c.k + H / 2 - (11 * Math.sqrt(n) + 14) * c.k
        g.font = `600 ${Math.min(20, 11.5 + Math.sqrt(n))}px var(--sans), sans-serif`
        g.fillStyle = 'rgba(26,26,26,.42)'
        g.fillText(compName[key] || key, ax, ay)
        g.font = '10px var(--sans), sans-serif'
        g.fillStyle = 'rgba(26,26,26,.32)'
        g.fillText(`${n} 个事件`, ax, ay + 13)
      }
    }

    // ── 边 ──
    for (const e of view.edges) {
      const a = idx.get(e.f), b = idx.get(e.t)
      if (a == null || b == null) continue
      const r = relOf(e.r)
      const on = !dim || (lit.has(e.f) && lit.has(e.t))
      if (dim && !on) continue                 // 暗化时直接不画，比半透明更干净
      g.save()
      g.strokeStyle = r.c
      g.globalAlpha = r.rule ? (dim ? 1 : 0.75) : 0.92
      g.lineWidth = r.rule ? 1.1 : 2.2
      g.setLineDash(r.dash || [])
      const x1 = SX(a), y1 = SY(a), x2 = SX(b), y2 = SY(b)
      g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke()
      g.setLineDash([])
      if (r.dir) arrow(g, x1, y1, x2, y2, radius(view.deg.get(e.t)) * c.k + 3, r.c)
      g.restore()
    }

    // ── 节点 ──
    // 实心点而不是空心圈。第一版画的是白底 + 1.5px 灰描边的圆环，描边和边线
    // 一样粗，于是满屏都是圈、看不见连线——圆圈抢走了本该属于边的视觉权重。
    // 现在节点是实心色块、外面套一圈白描边（重叠时互相分开），线就浮出来了。
    for (let i = 0; i < view.nodes.length; i++) {
      const nd = view.nodes[i]
      const on = !dim || lit.has(nd.id)
      const sx = SX(i), sy = SY(i)
      const rr = radius(view.deg.get(nd.id)) * Math.max(1, Math.sqrt(c.k))
      const solid = nd.sdeg > 0          // 有语义边的事件：深色，一眼找得到
      g.beginPath(); g.arc(sx, sy, rr, 0, Math.PI * 2)
      g.fillStyle = on
        ? (solid ? '#1A1A1A' : '#B4B4B4')
        : (solid ? '#D5D5D5' : '#ECECEC')
      g.fill()
      g.lineWidth = 1.5
      g.strokeStyle = '#FFFFFF'
      g.stroke()
      if (nd.id === sel) {
        g.beginPath(); g.arc(sx, sy, rr + 4.5, 0, Math.PI * 2)
        g.strokeStyle = '#1A1A1A'; g.lineWidth = 1.5; g.stroke()
      }
    }

    // ── 标题：只给焦点节点及其邻居，或放大到一定倍数后给高度数节点 ──
    // 记下已占用的矩形，压不进去的标签直接不画：邻居挨得近时三四个标签会叠在
    // 同一处糊成一团，宁可少标几个也不能叠
    g.font = '11px var(--sans), sans-serif'
    g.textAlign = 'center'
    const taken = []
    const free = (x0, y0, x1, y1) =>
      !taken.some((r) => x0 < r[2] && x1 > r[0] && y0 < r[3] && y1 > r[1])

    const order = [...view.nodes.keys()].sort((a, b) => {
      const A = view.nodes[a].id === focusId ? 2 : lit.has(view.nodes[a].id) ? 1 : 0
      const B = view.nodes[b].id === focusId ? 2 : lit.has(view.nodes[b].id) ? 1 : 0
      return B - A || view.deg.get(view.nodes[b].id) - view.deg.get(view.nodes[a].id)
    })
    for (const i of order) {
      const nd = view.nodes[i]
      const show = lit.has(nd.id) || (!dim && c.k > 1.5 && view.deg.get(nd.id) >= 3)
      if (!show) continue
      const sx = SX(i), sy = SY(i) + radius(view.deg.get(nd.id)) + 13
      const t = nd.t.length > 16 ? nd.t.slice(0, 16) + '…' : nd.t
      const w = g.measureText(t).width
      const box = [sx - w / 2 - 4, sy - 11, sx + w / 2 + 4, sy + 4]
      if (!free(...box)) continue
      taken.push(box)
      g.fillStyle = 'rgba(255,255,255,.9)'
      g.fillRect(box[0], box[1], box[2] - box[0], box[3] - box[1])
      g.fillStyle = nd.id === focusId ? '#1A1A1A' : '#4A4A4A'
      g.fillText(t, sx, sy)
    }
  }, [view, hover, sel, comp, compName])

  useEffect(() => { draw() }, [draw])

  /* ── 命中测试与交互 ── */
  const pick = (ev) => {
    const cv = canvas.current, s = st.current
    if (!cv || !s || !view) return null
    const b = cv.getBoundingClientRect()
    const mx = ev.clientX - b.left, my = ev.clientY - b.top
    const c = cam.current
    let best = null, bd = 1e9
    for (let i = 0; i < view.nodes.length; i++) {
      const sx = (s.x[i] - c.x) * c.k + b.width / 2
      const sy = (s.y[i] - c.y) * c.k + b.height / 2
      const d = (sx - mx) ** 2 + (sy - my) ** 2
      const rr = radius(view.deg.get(view.nodes[i].id)) + 7
      if (d < rr * rr && d < bd) { bd = d; best = view.nodes[i] }
    }
    return best
  }

  const onMove = (ev) => {
    if (drag.current) {
      const c = cam.current
      c.x -= (ev.clientX - drag.current.px) / c.k
      c.y -= (ev.clientY - drag.current.py) / c.k
      drag.current = { px: ev.clientX, py: ev.clientY, moved: true }
      draw()
      return
    }
    const n = pick(ev)
    const id = n?.id || null
    if (id !== hover) setHover(id)
    if (canvas.current) canvas.current.style.cursor = n ? 'pointer' : 'grab'
  }

  const onUp = (ev) => {
    const moved = drag.current?.moved
    drag.current = null
    if (moved) return                     // 拖拽平移，不当作点击
    const n = pick(ev)
    setSel(n ? (n.id === sel ? null : n.id) : null)
  }

  const onWheel = (ev) => {
    ev.preventDefault()
    const c = cam.current
    const k = Math.min(4, Math.max(0.3, c.k * (ev.deltaY < 0 ? 1.12 : 1 / 1.12)))
    // 以光标为锚点缩放，否则放大时目标会跑出视野
    const b = canvas.current.getBoundingClientRect()
    const wx = (ev.clientX - b.left - b.width / 2) / c.k + c.x
    const wy = (ev.clientY - b.top - b.height / 2) / c.k + c.y
    c.x = wx - (ev.clientX - b.left - b.width / 2) / k
    c.y = wy - (ev.clientY - b.top - b.height / 2) / k
    c.k = k
    draw()
  }

  // Chrome 下 passive 监听器里 preventDefault 无效，滚轮缩放会连带滚页面，
  // 必须手动以 passive:false 绑定
  useEffect(() => {
    const cv = canvas.current
    if (!cv) return
    const h = (e) => onWheel(e)
    cv.addEventListener('wheel', h, { passive: false })
    return () => cv.removeEventListener('wheel', h)
  })

  if (err) return <div className="error">加载失败：{err}　图谱需要联网获取快照。</div>
  if (!data) return <Loading />

  const focus = sel && view?.byId.get(sel)
  const links = dedupe(sel ? (view.adj.get(sel) || []) : [])
  const sem = links.filter((l) => l.by !== 'rule')
  const rule = links.filter((l) => l.by === 'rule')
  const s = data.stats || {}

  return (
    <div className="view graph-view">
      <div className="graph-head">
        <div>
          <h2 className="sec-title">知识图谱</h2>
          <p className="graph-sub">
            {s.nodes} 个事件 · {s.edges} 条边（语义 {s.semantic} / 规则 {s.rule}）
            {s.isolated ? ` · 孤立 ${s.isolated}` : ''}
          </p>
        </div>
        <div className="graph-ctl">
          <button className={`fchip ${semOnly ? 'on' : ''}`} onClick={() => { setSel(null); setSemOnly((v) => !v) }}>
            只看语义关联
          </button>
          <button className="fchip" onClick={() => {
            const cv = canvas.current
            if (cv && st.current) cam.current = fitView(st.current, cv.clientWidth, cv.clientHeight)
            draw()
          }}>
            复位视角
          </button>
        </div>
      </div>

      <div className="frow graph-companies">
        <span className="flabel">公司</span>
        {(meta?.companies || []).map((c) => (
          <button key={c.id} className={`fchip ${comp.has(c.id) ? 'on' : ''}`}
                  onClick={() => setComp((p) => {
                    const n = new Set(p); n.has(c.id) ? n.delete(c.id) : n.add(c.id); return n
                  })}>{c.name}</button>
        ))}
        {comp.size > 0 && <button className="fclear" onClick={() => setComp(new Set())}>清除 {comp.size}</button>}
      </div>

      {/* 图例常驻：≥2 个类别必须有图例，身份不能只靠颜色 */}
      <div className="graph-legend">
        {LEGEND.map((l) => (
          <span key={l.label} className="lg">
            <svg width="26" height="10" aria-hidden>
              <line x1="1" y1="5" x2={l.arrow ? 18 : 25} y2="5" stroke={l.c}
                    strokeWidth={l.c === '#C2C2C2' ? 1.1 : 1.8}
                    strokeDasharray={l.dash ? '5 4' : undefined} />
              {l.arrow && <path d="M18 1.5 L25 5 L18 8.5 Z" fill={l.c} />}
            </svg>
            {l.label}
          </span>
        ))}
        <span className="lg"><i className="dot solid" />有语义关联</span>
        <span className="lg"><i className="dot hollow" />仅规则边</span>
      </div>

      {!view?.nodes.length ? (
        <Empty>
          {semOnly
            ? '还没有任何语义关联边。S6b 需要 JINA_API_KEY 才能召回候选对，跑过之后这里才会有内容。'
            : '当前筛选下没有可显示的事件'}
        </Empty>
      ) : (
        <div className="graph-stage" ref={wrap}>
          <canvas
            ref={canvas}
            onMouseDown={(e) => { drag.current = { px: e.clientX, py: e.clientY, moved: false } }}
            onMouseMove={onMove}
            onMouseUp={onUp}
            onMouseLeave={() => { drag.current = null; setHover(null) }}
          />
          {progress < 1 && (
            <div className="graph-progress"><span style={{ width: `${progress * 100}%` }} /></div>
          )}
          <div className="graph-hint">滚轮缩放 · 拖拽平移 · 点击节点看关联</div>

          {focus && (
            <aside className="graph-panel">
              <button className="gp-close" onClick={() => setSel(null)} aria-label="关闭">×</button>
              <div className="gp-co">{compName[focus.c] || focus.c} · {focus.dt || '日期未知'}</div>
              <h3 className="gp-title">{focus.t}</h3>
              <div className="gp-meta">{focus.s} · {focus.cf}</div>

              {sem.length > 0 && (
                <>
                  <div className="gp-sub">语义关联 {sem.length}</div>
                  <ul className="gp-links">
                    {sem.map((l, i) => (
                      <li key={i}>
                        <button className="gp-link" onClick={() => setSel(l.other)}>
                          <span className="gp-rel" style={{ color: relOf(l.rel).c }}>
                            {l.dir === 'out' && relOf(l.rel).dir ? '→ ' : ''}
                            {l.dir === 'in' && relOf(l.rel).dir ? '← ' : ''}
                            {relOf(l.rel).label}
                          </span>
                          {view.byId.get(l.other)?.t || l.other}
                        </button>
                        {l.basis && <div className="gp-basis">{l.basis}</div>}
                      </li>
                    ))}
                  </ul>
                </>
              )}

              {rule.length > 0 && (
                <>
                  <div className="gp-sub">同主体动作 {rule.length}</div>
                  <ul className="gp-links rule">
                    {rule.map((l, i) => (
                      <li key={i}>
                        <button className="gp-link" onClick={() => setSel(l.other)}>
                          {view.byId.get(l.other)?.t || l.other}
                        </button>
                      </li>
                    ))}
                  </ul>
                </>
              )}

              {!links.length && <p className="gp-basis">这个事件还没有任何关联边。</p>}
              <button className="btn gp-go" onClick={() => nav(`/events/${focus.id}`)}>
                查看事件详情 →
              </button>
            </aside>
          )}
        </div>
      )}

      {s.semantic < 20 && (
        <p className="graph-note">
          语义边目前只有 {s.semantic} 条。它们由 S6b 每轮向量召回「相近但不是同一件事」的事件对、
          再交模型逐对判关系而来，每轮只问本轮动过的事件，所以图是<strong>随轮次逐步长出来的</strong>。
          其余 {s.rule} 条是规则边：按时间把同一家公司的事件串成一条链，只给顺序不给关系，
          图上那些浅灰细线就是它们，也正是公司分岛的由来。
        </p>
      )}
    </div>
  )
}

/** 同一对事件之间可能存了不止一条边（两个方向各一条，或规则边与语义边并存）。
 *  侧栏按「对端 + 关系」去重，否则同一个关联会在列表里出现两遍。 */
function dedupe(links) {
  const seen = new Set()
  return links.filter((l) => {
    const k = `${l.other}|${l.rel}`
    if (seen.has(k)) return false
    seen.add(k)
    return true
  })
}

/* 度数 → 半径。开方而不是线性：度数 8 的节点面积是度数 2 的 4 倍已经够醒目，
   线性会让它大到盖住邻居 */
function radius(deg = 0) {
  return Math.min(11, 2.8 + Math.sqrt(deg) * 1.8)
}

/** 有向边的箭头。画在目标节点边缘而不是圆心，否则会被节点盖住看不见。 */
function arrow(g, x1, y1, x2, y2, back, color) {
  const dx = x2 - x1, dy = y2 - y1
  const d = Math.hypot(dx, dy) || 1
  const ux = dx / d, uy = dy / d
  const tx = x2 - ux * back, ty = y2 - uy * back
  const size = 7
  g.beginPath()
  g.moveTo(tx, ty)
  g.lineTo(tx - ux * size + -uy * size * 0.45, ty - uy * size + ux * size * 0.45)
  g.lineTo(tx - ux * size - -uy * size * 0.45, ty - uy * size - ux * size * 0.45)
  g.closePath()
  g.fillStyle = color
  g.fill()
}
