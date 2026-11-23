// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { PlainGuide, ACTION_CARD_TERMS, PORTFOLIO_RISK_TERMS, POSITION_OVERVIEW_TERMS } from './PlainGuide'

let host: HTMLDivElement
let root: Root

const terms = [
  { term: '权重', explain: '占账户的比例' },
  { term: '集中度', explain: '钱压在少数票上' },
  { term: '回撤', explain: '从高点亏了多少' },
  { term: '相关性', explain: '是否同涨同跌' },
  { term: '波动', explain: '上下晃多大幅度' },
]

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

async function render(ts = terms) {
  await act(async () => root.render(
    <PlainGuide intro="这是一段给普通投资者看的大白话" terms={ts} />,
  ))
}

function button(text: string) {
  return [...host.querySelectorAll('button')].find(b => b.textContent?.includes(text))
}

it('默认直接摊开前 3 个常用词，其余收进折叠区', async () => {
  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('这是一段给普通投资者看的大白话')
  // 前 3 个不用点开就能看到
  expect(text).toContain('占账户的比例')
  expect(text).toContain('钱压在少数票上')
  expect(text).toContain('从高点亏了多少')
  // 后 2 个默认不出现
  expect(text).not.toContain('是否同涨同跌')
  expect(text).not.toContain('上下晃多大幅度')
  expect(button('其余 2 个名词')).toBeTruthy()
})

it('点开后其余名词出现，再点收起又消失', async () => {
  await render()
  const expand = button('其余 2 个名词')!
  await act(async () => expand.click())
  expect(host.textContent).toContain('是否同涨同跌')
  expect(host.textContent).toContain('上下晃多大幅度')
  expect(expand.getAttribute('aria-expanded')).toBe('true')

  const collapse = button('收起其余名词')!
  await act(async () => collapse.click())
  expect(host.textContent).not.toContain('是否同涨同跌')
  expect(host.textContent).not.toContain('上下晃多大幅度')
})

it('名词不超过 3 个时全部直接摊开，不显示折叠按钮', async () => {
  await render(terms.slice(0, 2))
  const text = host.textContent ?? ''
  expect(text).toContain('占账户的比例')
  expect(text).toContain('钱压在少数票上')
  expect(host.querySelector('button')).toBeNull()
})

it('三页词表都有内容，且术语与解释成对', () => {
  expect(ACTION_CARD_TERMS.length).toBeGreaterThan(3)
  expect(PORTFOLIO_RISK_TERMS.length).toBeGreaterThan(3)
  expect(POSITION_OVERVIEW_TERMS.length).toBeGreaterThan(3)
  for (const t of [...ACTION_CARD_TERMS, ...PORTFOLIO_RISK_TERMS, ...POSITION_OVERVIEW_TERMS]) {
    expect(t.term.length).toBeGreaterThan(0)
    expect(t.explain.length).toBeGreaterThan(0)
  }
})
