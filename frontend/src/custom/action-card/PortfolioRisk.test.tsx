// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { PortfolioRisk } from './PortfolioRisk'

const mocks = vi.hoisted(() => ({
  manualHoldings: vi.fn(),
  watchlistList: vi.fn(),
  portfolioRiskByManual: vi.fn(),
  portfolioRisk: vi.fn(),
  instrumentNames: vi.fn(),
}))

vi.mock('@/lib/api', () => ({ api: mocks }))
vi.mock('@/components/PageHeader', () => ({
  PageHeader: ({ title, subtitle }: { title: string; subtitle: string }) => (
    <div data-testid="page-header">{title}｜{subtitle}</div>
  ),
}))
vi.mock('@/components/data/Skeleton', () => ({
  Skeleton: () => <div data-testid="skeleton" />,
}))
vi.mock('@/components/signals/PortfolioRiskPanel', () => ({
  PortfolioRiskPanel: ({ manual, warnObservation }: { manual?: boolean; warnObservation?: boolean }) => (
    <div data-testid={manual ? 'risk-manual' : warnObservation ? 'risk-observation' : 'risk-other'} />
  ),
  BuySimulationCard: ({ manual }: { manual?: boolean }) => (
    <div data-testid={manual ? 'sim-manual' : 'sim-other'} />
  ),
}))
vi.mock('./ManualHoldingsPanel', () => ({
  ManualHoldingsPanel: () => <div data-testid="manual-holdings-panel">我的真实持仓录入面板</div>,
}))

let host: HTMLDivElement
let root: Root
let qc: QueryClient

const manualHoldingsResponse = {
  holdings: [], count: 1, cash: null,
  summary: {
    market_value: 90550, total: 90550, total_pnl: 20550,
    price_source: 'daily', position_count: 1, weight_basis: 'manual',
  },
  positions: [{
    symbol: '000333.SZ', name: '美的集团', qty: 1000, avg_cost: 70,
    last_price: 90.55, market_value: 90550, weight: 1, pnl: 20550,
    pnl_pct: 29.36, priced: true, holding_days: 30,
  }],
  limitations: [],
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  qc = new QueryClient({ defaultOptions: {
    queries: { retry: false },
    mutations: { retry: false },
  } })
  mocks.manualHoldings.mockResolvedValue(manualHoldingsResponse)
  mocks.watchlistList.mockResolvedValue({ symbols: [
    { symbol: '600941.SH', added_at: '' },
    { symbol: '605366.SH', added_at: '' },
    { symbol: '000333.SZ', added_at: '' },
    { symbol: '600941.SH', added_at: '' }, // 重复项要去掉
  ] })
  mocks.portfolioRiskByManual.mockResolvedValue({})
  mocks.portfolioRisk.mockResolvedValue({})
  mocks.instrumentNames.mockResolvedValue({ names: {} })
})

afterEach(async () => {
  await act(async () => root.unmount())
  qc.clear()
  host.remove()
  vi.clearAllMocks()
})

async function flush() {
  await act(async () => { await new Promise(r => setTimeout(r, 60)) })
}

function button(text: string) {
  return [...host.querySelectorAll('button')].find(b => b.textContent?.includes(text))!
}

it('页面顶部固定放「我的真实持仓」录入面板，真仓口径自动出风险与买入模拟', async () => {
  await act(async () => root.render(
    <QueryClientProvider client={qc}><PortfolioRisk /></QueryClientProvider>,
  ))
  await flush()

  expect(host.querySelector('[data-testid="manual-holdings-panel"]')).toBeTruthy()
  expect(host.textContent).toContain('组合风控')
  expect(host.textContent).toContain('只有真实持仓计入盈亏与权重')
  // 面向普通投资者：页面挂了放大字体的作用域类，且顶部有白话引导 + 常用名词直接摊开
  expect(host.querySelector('.tsp-readable')).toBeTruthy()
  expect(host.textContent).toContain('体检')
  expect(host.textContent).toContain('权重')
  expect(host.textContent).toContain('占你整个账户的比例')
  expect(host.querySelector('[data-testid="risk-manual"]')).toBeTruthy()
  expect(host.querySelector('[data-testid="sim-manual"]')).toBeTruthy()
  // 没导入自选中选时，观察口径不出面板
  expect(host.querySelector('[data-testid="risk-observation"]')).toBeNull()
  expect(host.textContent).toContain('点「导入自选」')
})

it('没录入真仓时如实提示，不渲染真仓风险面板', async () => {
  mocks.manualHoldings.mockResolvedValue({
    ...manualHoldingsResponse, count: 0, positions: [],
  })
  await act(async () => root.render(
    <QueryClientProvider client={qc}><PortfolioRisk /></QueryClientProvider>,
  ))
  await flush()

  expect(host.textContent).toContain('还没有录入真实持仓')
  expect(host.querySelector('[data-testid="risk-manual"]')).toBeNull()
  // 但顶部录入面板仍在
  expect(host.querySelector('[data-testid="manual-holdings-panel"]')).toBeTruthy()
})

it('导入自选后走观察口径、去重、并明确「自选 ≠ 持仓」', async () => {
  await act(async () => root.render(
    <QueryClientProvider client={qc}><PortfolioRisk /></QueryClientProvider>,
  ))
  await flush()

  await act(async () => button('导入自选').click())
  await flush()

  expect(mocks.watchlistList).toHaveBeenCalledTimes(1)
  expect(host.querySelector('[data-testid="risk-observation"]')).toBeTruthy()
  const text = host.textContent ?? ''
  expect(text).toContain('已带入 3 只自选')
  expect(text).toContain('自选 ≠ 持仓')
  expect(text).toContain('按等权假设测算')
  // 真仓口径依然在，两种口径互不覆盖
  expect(host.querySelector('[data-testid="risk-manual"]')).toBeTruthy()
  // 真仓面板在观察面板之前（顶部优先）
  const manualEl = host.querySelector('[data-testid="manual-holdings-panel"]')!
  const obsEl = host.querySelector('[data-testid="risk-observation"]')!
  expect(manualEl.compareDocumentPosition(obsEl) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
})

it('清空自选后回到未导入提示', async () => {
  await act(async () => root.render(
    <QueryClientProvider client={qc}><PortfolioRisk /></QueryClientProvider>,
  ))
  await flush()
  await act(async () => button('导入自选').click())
  await flush()
  await act(async () => button('清空').click())
  await flush()

  expect(host.querySelector('[data-testid="risk-observation"]')).toBeNull()
  expect(host.textContent).toContain('点「导入自选」')
})

it('自选读取失败时给出明确错误，不假装导入成功', async () => {
  mocks.watchlistList.mockRejectedValue(new Error('后端未启动'))
  await act(async () => root.render(
    <QueryClientProvider client={qc}><PortfolioRisk /></QueryClientProvider>,
  ))
  await flush()
  await act(async () => button('导入自选').click())
  await flush()

  expect(host.textContent).toContain('自选读取失败')
  expect(host.textContent).toContain('后端未启动')
  expect(host.querySelector('[data-testid="risk-observation"]')).toBeNull()
})
