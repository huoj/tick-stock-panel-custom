// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { PortfolioRiskPanel } from './PortfolioRiskPanel'
import type { ManualHoldingPosition, PortfolioRiskReport } from '@/lib/api'

/**
 * 组合风控页「一眼看到关键信息」的决策层回归。
 *
 * 夹具全部取自 **2026-11-16 11:26 对 3018 的真实响应**
 * （`GET /api/risk/portfolio?source=manual` 9 只持仓 + `GET /api/manual-holdings`
 * 的同批 positions），不是编的：这样以后谁把量纲改错、或把明细渲染删掉，
 * 这些断言会先红。
 *
 * 断言只写「用户在页面上看得见的中文文案与数字」，不碰内部字段名。
 */

const mocks = vi.hoisted(() => ({
  portfolioRiskByManual: vi.fn(),
  instrumentNames: vi.fn(),
}))

vi.mock('@/lib/api', () => ({ api: mocks }))

/** 真实报告的四个关键子结构（其余字段与本次改动无关，按最小集给）。 */
const realReport: PortfolioRiskReport = {
  positions: [
    { symbol: '000333.SZ', weight: 0.262, drawdown_pct: 0.087, industry: '白色家电-空调' },
    { symbol: '000651.SZ', weight: 0.2283, drawdown_pct: 0.1206, industry: '白色家电-空调' },
    { symbol: '159529.SZ', weight: 0.1297, drawdown_pct: 0.0537, industry: null },
    { symbol: '601633.SH', weight: 0.082, drawdown_pct: 0.1794, industry: '汽车整车-乘用车' },
    { symbol: '605366.SH', weight: 0.0246, drawdown_pct: 0.3922, industry: '化学制品-有机硅' },
  ],
  industry_concentration: {
    by_industry: { '白色家电-空调': 0.49028, '汽车整车-乘用车': 0.082023, '化学制品-有机硅': 0.024641 },
    unclassified_weight: 0.2451,
    hhi: 0.2602,
    effective_industries: 3.84,
    top_industry: '白色家电-空调',
    top_industry_share: 0.4903,
    warnings: [
      '最大行业「白色家电-空调」占 49.0%，超过警戒线 35%',
      '24.5% 的权重没有行业归属，集中度结论不完整',
    ],
  },
  correlation: {
    average_correlation: 0.1106,
    highest_pair: { a: '159586.SZ', b: '300059.SZ', correlation: 0.7031 },
    lowest_pair: { a: '000333.SZ', b: '159682.SZ', correlation: -0.4886 },
    effective_bets: 3.96,
    pair_count: 36,
    warnings: ['159586.SZ 与 300059.SZ 相关系数 0.70，接近同一笔风险'],
    common_days: 59,
  },
  per_position_risk: {
    positions: [
      { symbol: '000651.SZ', industry: '白色家电-空调', weight: 0.2283, drawdown_pct: 0.1206, risk_contribution: 0.0275 },
      { symbol: '000333.SZ', industry: '白色家电-空调', weight: 0.262, drawdown_pct: 0.087, risk_contribution: 0.0228 },
      { symbol: '605366.SH', industry: '化学制品-有机硅', weight: 0.0246, drawdown_pct: 0.3922, risk_contribution: 0.0097 },
      { symbol: '601633.SH', industry: '汽车整车-乘用车', weight: 0.082, drawdown_pct: 0.1794, risk_contribution: 0.0147 },
      { symbol: '159529.SZ', industry: null, weight: 0.1297, drawdown_pct: 0.0537, risk_contribution: 0.007 },
    ],
    total_risk_contribution: 0.1302,
    warnings: [],
  },
  drawdown_budget: {
    budget_pct: 15,
    stressed_drawdown_pct: 13.0231,
    budget_usage_pct: 86.82,
    over_budget: false,
    suggested_cuts: [],
    warnings: ['压力回撤 13.02% 已用掉预算的 87%，接近上限'],
  },
  // 真实口径是 0~1 小数：0.663 表示 66.3%。旧代码直接 toFixed(0) 会显示成「1%」甚至「0%」。
  volatility: {
    '000333.SZ': 0.2125,
    '000651.SZ': 0.2246,
    '159529.SZ': 0.239,
    '601633.SH': 0.2181,
    '605366.SH': 0.663,
  },
  cash_ratio: 0,
  invested_ratio: 1,
  basis: 'manual_positions',
  basis_label: '手工录入的真实持仓（未对账）',
  weight_basis: 'market_value / (cash + market_value)',
  positions_fed: 9,
  account_id: null,
  account_name: '手工录入',
  account_equity: 140010,
  account_cash: 0,
  account_market_value: 140010,
  total_pnl: -3439.59,
  max_drawdown: null,
  current_drawdown: null,
  nav_series_limit: null,
  nav_limitation: '手工录入的持仓没有历史净值序列，账户级回撤与基准对比无法计算；上面的回撤是「持仓层面按日线算的」，不是账户走过的净值',
  limitations: [
    '没有录入现金余额，权重合计会等于 1（看起来像满仓）—— 若你还有现金或其它资产没录，实际仓位比这里显示的更低',
    '1 笔持仓成本为负（做 T 摊薄后的正常结果）：盈亏金额与权重照常按现价算，收益率(%)因成本为负不适用、显示「—」',
    '压力回撤取历史实现最大回撤，不是未来损失上限',
    '8 笔持仓没有填建仓日期，持仓期限按缺失处理',
    '估值用各标的最近一根日线收盘价',
  ],
  data_quality: {
    as_of: '2026-11-16',
    data_through: '2026-11-13',
    common_days: 58,
    flags: [],
    stale: false,
    insufficient: false,
    verdict: 'ok',
    note: '数据完整且新鲜, 组合风险测算可作为参考。',
    limitations: ['仅统计 symbols 列出标的的收益序列, 其余输入不参与评估'],
  },
  relative_performance: {
    period_return: -0.038947351920481266,
    benchmark_return: -0.15287787765618843,
    excess_return: 0.11393052573570717,
    beta: 0.2656141541210581,
    alpha: 9.576279324153842e-05,
    tracking_error: 0.009655487294177196,
    information_ratio: 0.24018664373386325,
    sharpe: -0.0954801373462302,
    common_days: 54,
    as_of: '2026-11-13',
    limitations: ['仅使用共同交易日: 组合 4 日、基准 0 日未参与计算'],
  },
} as unknown as PortfolioRiskReport

/** 与上面同一时刻 `GET /api/manual-holdings` 的 positions（真实值）。 */
const realManualPositions: ManualHoldingPosition[] = [
  {
    symbol: '000333.SZ', name: '美的集团', asset_type: 'stock', qty: 400, available_qty: null,
    avg_cost: -33.3896, last_price: 91.69, priced: true, market_value: 36676,
    weight: 0.261953, pnl: 50031.84, pnl_pct: null, entry_date: '2017-06-27',
    holding_days: 3429, note: '',
  },
  {
    symbol: '000651.SZ', name: '格力电器', asset_type: 'stock', qty: 800, available_qty: null,
    avg_cost: 42.6718, last_price: 39.96, priced: true, market_value: 31968,
    weight: 0.228327, pnl: -2169.44, pnl_pct: -6.36, entry_date: null,
    holding_days: null, note: '',
  },
  {
    symbol: '159529.SZ', name: '食品饮料ETF', asset_type: 'etf', qty: 20000, available_qty: null,
    avg_cost: 1.5, last_price: 1.816, priced: true, market_value: 36320,
    weight: 0.2594, pnl: 6320, pnl_pct: 21.07, entry_date: '2024-03-01',
    holding_days: 626, note: '',
  },
  {
    symbol: '601633.SH', name: '长城汽车', asset_type: 'stock', qty: 700, available_qty: null,
    avg_cost: 25, last_price: 22.86, priced: true, market_value: 16002,
    weight: 0.1143, pnl: -1498, pnl_pct: -8.56, entry_date: null,
    holding_days: null, note: '',
  },
  {
    // 权重小但波动极大（66.3%，是第二名的 1.7 倍）—— 这种票必须能在页面上一眼看出来。
    symbol: '605366.SH', name: '宏柏新材', asset_type: 'stock', qty: 1000, available_qty: null,
    avg_cost: 12, last_price: 3.44, priced: true, market_value: 3440,
    weight: 0.0246, pnl: -8560, pnl_pct: -71.33, entry_date: null,
    holding_days: null, note: '',
  },
]

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  mocks.portfolioRiskByManual.mockResolvedValue(realReport)
  mocks.instrumentNames.mockResolvedValue({ names: {} })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function render(manualPositions?: ManualHoldingPosition[]) {
  await act(async () => root.render(
    <QueryClientProvider client={client}>
      <PortfolioRiskPanel manual autoRun manualPositions={manualPositions} />
    </QueryClientProvider>,
  ))
  for (let i = 0; i < 3; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
  }
  return host.textContent ?? ''
}

it('首屏先给一句结论 + 四盏灯，不滚动就知道整体危不危险', async () => {
  const text = await render(realManualPositions)
  // 行业集中 49%（超 35% 警戒线）+ 单票波动 66%（超 60%）→ 两项都判「需处理」。
  // 结论必须先给态度（几项有问题 + 具体是哪几项），而不是只丢一堆表格。
  expect(text).toContain('有 2 项需要处理')
  expect(text).toContain('行业集中 49% · 单票波动 66%')
  expect(text).toContain('行业集中')
  expect(text).toContain('49%')
  expect(text).toContain('白色家电-空调')
  expect(text).toContain('24.5% 无行业归属')
  // 回撤预算 86.82% → 接近上限
  expect(text).toContain('回撤预算')
  expect(text).toContain('87%')
  expect(text).toContain('压力回撤 13.0% / 预算 15%')
  // 结论带要带上账户权益与总盈亏，金额带「元」
  expect(text).toContain('账户权益')
  expect(text).toContain('14.00 万元')
  expect(text).toContain('总盈亏')
})

it('四盏灯可点，锚点落在对应的详情区块上', async () => {
  await render(realManualPositions)
  const anchors = [...host.querySelectorAll('a[href^="#risk-"]')]
  expect(anchors.length).toBeGreaterThanOrEqual(4)
  for (const a of anchors) {
    const id = a.getAttribute('href')!.slice(1)
    expect(host.querySelector(`#${id}`), `锚点 #${id} 必须有对应区块`).toBeTruthy()
  }
})

it('持仓速览把名称、现价、市值、盈亏一次摊开（这些数据早就请求过，此前只用了 count）', async () => {
  const text = await render(realManualPositions)
  expect(text).toContain('持仓速览')
  // 名称 + 代码
  expect(text).toContain('美的集团')
  expect(text).toContain('格力电器')
  // 现价、市值、盈亏金额
  expect(text).toContain('现价 91.69')
  expect(text).toContain('3.67 万元')
  expect(text).toContain('5.00 万元')
  expect(text).toContain('已持 3429 天')
  // 亏损的票必须用红色语义标出（-2169 元）
  const lossRow = [...host.querySelectorAll('tr')].find(r => r.textContent?.includes('格力电器'))
  expect(lossRow?.textContent).toContain('-6.36%')
  expect(lossRow?.querySelector('.text-danger')).toBeTruthy()
})

it('按风险贡献降序：最该先看的排在表格最上面', async () => {
  await render(realManualPositions)
  const rows = [...host.querySelectorAll('tr')].filter(r => r.querySelector('td'))
  const order = rows.map(r => r.textContent ?? '').join('|')
  // 风险贡献 0.0275（格力）> 0.0228（美的）> 0.0147（长城）> 0.0097（宏柏）> 0.0070（ETF）
  expect(order.indexOf('格力电器')).toBeLessThan(order.indexOf('美的集团'))
  expect(order.indexOf('美的集团')).toBeLessThan(order.indexOf('长城汽车'))
})

it('年化波动按小数口径换算：66.3% 不能被显示成 1% 或 0%', async () => {
  const text = await render(realManualPositions)
  // 旧代码 `volatility[symbol].toFixed(0)` 会把这些全显示成 0%，红色阈值 >60 永不触发
  expect(text).toContain('66%')
  expect(text).toContain('21%')
  expect(text).toContain('22%')
  expect(text).not.toMatch(/年化波动[\s\S]{0,80}0%/)
  // 波动偏高要直接标出来，而不是让用户自己比大小
  const row = [...host.querySelectorAll('tr')].find(r => r.textContent?.includes('宏柏新材'))
  expect(row?.textContent).toContain('波动偏高')
  expect(row?.querySelector('.text-danger')).toBeTruthy()
})

it('潜在回撤按小数口径换算：39.22% 不能显示成 0.39%', async () => {
  await render(realManualPositions)
  const row = [...host.querySelectorAll('tr')].find(r => r.textContent?.includes('宏柏新材'))
  // 旧代码走 pctNum（注释「后端给的是百分数」）→ 显示 0.39%，缩小 100 倍
  expect(row?.textContent).toContain('39.2%')
  expect(row?.textContent).not.toContain('0.39%')
  const row2 = [...host.querySelectorAll('tr')].find(r => r.textContent?.includes('美的集团'))
  expect(row2?.textContent).toContain('8.7%')
})

it('负成本那笔如实说明「成本为负」，不让人以为系统算坏了', async () => {
  const text = await render(realManualPositions)
  // 美的 avg_cost = -33.3896 → pnl_pct 为 null
  const row = [...host.querySelectorAll('tr')].find(r => r.textContent?.includes('美的集团'))
  expect(row?.textContent).toContain('成本为负')
  expect(text).toContain('做 T 把成本摊薄后的正常结果')
  expect(text).toContain('别把它读成「没算出来」')
})

it('没录现金时把「看似满仓」的误解写在明面上，不只藏在折叠框里', async () => {
  const text = await render(realManualPositions)
  expect(text).toContain('现金还没录入，仓位比例是按「满仓」估算的，不是实际满仓')
  expect(text).toContain('未录入')
  expect(text).toContain('（未录现金时的推导值）')
})

it('低 beta 的「跑赢」被翻译成「跌得少」，不让用户读成选股能力', async () => {
  const text = await render(realManualPositions)
  expect(text).toContain('11.39%')
  expect(text).toContain('跌得比大盘少')
  expect(text).toContain('不是选股超额')
  expect(text).toContain('别把少亏读成赚钱')
})

it('六个专家增强块默认收起，不再占据首屏之后的大段滚动', async () => {
  await render(realManualPositions)
  const adv = host.querySelector('details#risk-advanced')
  expect(adv).toBeTruthy()
  expect(adv!.hasAttribute('open')).toBe(false)
  expect(adv!.textContent).toContain('进阶指标')
  expect(adv!.textContent).toContain('不影响上面结论')
})

it('口径与限制不再折叠到底部：前 4 条直接可见', async () => {
  await render(realManualPositions)
  const block = host.querySelector('details#risk-limitations')!
  expect(block.hasAttribute('open')).toBe(true)
  expect(block.textContent).toContain('没有录入现金余额')
  expect(block.textContent).toContain('成本为负')
  expect(block.textContent).toContain('展开其余 1 条')
})

it('用量是百分数口径，不能被 asPct 二次放大（0.67% 不是 67%）', async () => {
  // 压力回撤极小（0.1% 仓位 × 1% 回撤）时 budget_usage_pct 会是 0.67 这种小数字。
  // 后端 `round(used * 100, 2)` 已经乘过 100，若再走 asPct（≤1 当小数 ×100）
  // 就会显示成 67%，把「几乎没用预算」误报成「用掉三分之二」。
  mocks.portfolioRiskByManual.mockResolvedValue({
    ...realReport,
    drawdown_budget: {
      budget_pct: 15,
      stressed_drawdown_pct: 0.1,
      budget_usage_pct: 0.67,
      over_budget: false,
      suggested_cuts: [],
      warnings: [],
    },
  } as unknown as PortfolioRiskReport)
  const text = await render(realManualPositions)
  expect(text).toContain('用量 1%')
  expect(text).not.toContain('用量 67%')
})

it('没有持仓明细时退化说明，而不是空白或假数据', async () => {
  const text = await render(undefined)
  expect(text).toContain('持仓速览')
  expect(text).toContain('这一栏没有名称、现价与盈亏')
  // 权重/波动/回撤仍然在（来自报告本身）
  expect(text).toContain('49%')
  expect(text).toContain('66%')
})
