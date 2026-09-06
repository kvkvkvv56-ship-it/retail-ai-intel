import React from 'react'

/** 置信度标记：不用色块，用形状与文字——瑞士版式靠层级不靠色彩 */
export function Confidence({ value }) {
  const solid = value === '官方确认·多源印证' || value === '多源已验证' || value === '官方一手'
  return (
    <span className={`conf ${solid ? 'conf-solid' : 'conf-weak'}`}>
      {value || '—'}
    </span>
  )
}

export function Status({ value }) {
  return <span className={`st st-${value === '新增' ? 'new' : value === '延续' ? 'cont' : 'old'}`}>{value}</span>
}

export function Empty({ children }) {
  return <p className="empty">{children}</p>
}

export function Rule() {
  return <hr className="rule" />
}

/** 时间口径标记（§6.3）：inferred 不参与窗口硬判定，须显式标出 */
export function TimeMark({ at, source }) {
  if (!at) return <span className="tmark">时间缺失</span>
  const label = { exact: '', parsed: '解析', inferred: '约' }[source] ?? ''
  return (
    <span className="tmark" title={`时间来源：${source || '未知'}`}>
      {label}{at.slice(0, 10)}
    </span>
  )
}
