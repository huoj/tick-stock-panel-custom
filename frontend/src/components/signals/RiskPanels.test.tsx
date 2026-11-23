// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type BuySimulationReport, type PortfolioRiskReport, type SignalRisk } from '@/lib/api'
import { SignalRiskCard } from './SignalRiskCard'
import { PortfolioRiskPanel, BuySimulationCard } from './PortfolioRiskPanel'

vi.mock('@/lib/api', () => ({ api: {
  signalsRisk: vi.fn(), signalRisk: vi.fn(), portfolioRisk: vi.fn(),
  portfolioRiskByAccount: vi.fn(), portfolioRiskByManual: vi.fn(), buySimulation: vi.fn(),
  instrumentNames: vi.fn(),
} }))

const trusted: SignalRisk = {
  signal_id: 'boll_breakout_upper',
  name: '突破布林上轨',
  kind: 'both',
  timeframe: 'daily',
  enabled: true,
  column: 'csg_boll_breakout_upper',
  horizon_days: 5,
  samples: 28642,
  win_rate: 0.3793,
  confidence_interval: { level: 0.95, low: 0.3737, high: 0.385, method: 'wilson' },
  excess_mean: -0.0001,
  excess_median: -0.002,
  potential_drawdown: -0.1537,
  best: 0.42,
  worst: -0.31,
  expected_holding_days: 5,
  liquidity: { bucket: 'mid', median_amount_cny: 294001156 },
  signal_score: {
    value: 23,
    meaning: '排序用强度指标（0-100），不是概率',
    components: ['效应量 40', '胜率区间下界 25', '样本充足度 20', '回撤惩罚 15'],
  },
  position_cap: {
    pct: 5.9,
    basis: '风险预算 1.5% ÷ 潜在回撤 15.4%；流动性 mid → ×0.6；钳制到 0.5~30% → 5.9%',
  },
  verdict: 'trusted',
  checked_window: { from: '2026-01-05', to: '2026-11-06' },
  reason: '收盘站上布林上轨且量能放大',
  falsify: '收盘跌回中轨下方',
  horizon_basis: '固定 5 个交易日',
  limitations: [],
}

const noSamples: SignalRisk = {
  ...trusted,
  signal_id: 'limit_up',
  samples: 0,
  win_rate: null,
  confidence_interval: { level: 0.95, low: null, high: null, method: 'wilson' },
  excess_mean: null,
  potential_drawdown: null,
  signal_score: { value: 0, meaning: '排序用强度指标（0-100），不是概率' },
  position_cap: { pct: 4.0, basis: '无回撤样本 → 默认 10%；流动性 unknown → ×0.4' },
  verdict: 'no_samples',
  reason: '',
  falsify: '',
  limitations: ['信号列 csg_limit_up 不在历史帧中'],
}

const report: PortfolioRiskReport = {
  positions: [
    { symbol: '600001.SH', weight: 0.6, drawdown_pct: 0.1, industry: '银行' },
    { symbol: '600002.SH', weight: 0.4, drawdown_pct: 0.2, industry: '白酒' },
  ],
  industry_concentration: {
    by_industry: { 银行: 0.6, 白酒: 0.4 },
    unclassified_weight: 0,
    hhi: 0.52,
    effective_industries: 1.92,
    top_industry: '银行',
    top_industry_share: 0.6,
    warnings: ['最大行业「银行」占 60.0%，超过警戒线 35%'],
  },
  correlation: {
    average_correlation: 0.73,
    highest_pair: { a: '600001.SH', b: '600002.SH', correlation: 0.73 },
    lowest_pair: null,
    effective_bets: 1.6,
    pair_count: 1,
    warnings: ['600001.SH 与 600002.SH 相关系数 0.73，接近同一笔风险'],
    common_days: 58,
  },
  per_position_risk: {
    positions: [
      { symbol: '600001.SH', industry: '银行', weight: 0.6, drawdown_pct: 0.1, risk_contribution: 0.06 },
      { symbol: '600002.SH', industry: '白酒', weight: 0.4, drawdown_pct: 0.2, risk_contribution: 0.08 },
    ],
    total_risk_contribution: 0.14,
    warnings: [],
  },
  drawdown_budget: {
    budget_pct: 15,
    stressed_drawdown_pct: 14,
    budget_usage_pct: 93.33,
    over_budget: false,
    suggested_cuts: [],
    warnings: ['压力回撤 14.00% 已用掉预算的 93%，接近上限'],
  },
  limitations: [],
}

const overBudget: PortfolioRiskReport = {
  ...report,
  drawdown_budget: {
    budget_pct: 15,
    stressed_drawdown_pct: 22,
    budget_usage_pct: 146.67,
    over_budget: true,
    suggested_cuts: [
      { symbol: '600002.SH', current_weight: 0.4, reduce_by: 0.4, new_weight: 0.0 },
    ],
    warnings: ['压力回撤 22.00% 超预算 15.00%（超出 7.00%）'],
  },
}

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // 面板现在会批量查代码→名称；给几个测试用得到的名称，才能断言「不再只有代码」。
  vi.mocked(api.instrumentNames).mockResolvedValue({ names: {
    '600001.SH': '浦发银行', '600002.SH': '齐鲁石化', '600941.SH': '中国移动', '000300.SH': '沪深300',
  } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle(ms = 20) {
  // 名称查询依赖报告结果（第二跳），多跑几轮 act 让依赖查询也 resolve 完，
  // 否则断言「代码旁出现名称」会在中间态失败。
  for (let i = 0; i < 3; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, ms)) })
  }
}

async function renderRisk(props: Parameters<typeof SignalRiskCard>[0]) {
  await act(async () => root.render(
    <QueryClientProvider client={client}><SignalRiskCard {...props} /></QueryClientProvider>,
  ))
  await settle()
}

async function renderPortfolio(props: Parameters<typeof PortfolioRiskPanel>[0]) {  await act(async () => root.render(
    <QueryClientProvider client={client}><PortfolioRiskPanel {...props} /></QueryClientProvider>,
  ))
  await settle()
}

async function clickButton(text: string) {
  const el = [...host.querySelectorAll('button')].find(b => b.textContent?.includes(text))
  if (!el) throw new Error(`未找到按钮「${text}」`)
  await act(async () => { el.click() })
  await settle()
}

// --- 信号风险卡片 ---------------------------------------------------------

it('把「信号分数」与「历史胜率」分开渲染，并带样本量与置信区间', async () => {
  await renderRisk({ risk: trusted })
  const text = host.textContent ?? ''
  expect(text).toContain('信号分数')
  expect(text).toContain('23')
  expect(text).toContain('排序用强度指标（0-100），不是概率')
  expect(text).toContain('历史胜率')
  expect(text).toContain('37.9%')
  // 区间与样本量必须同时出现
  expect(text).toContain('95% 区间')
  expect(text).toContain('37.4%')
  expect(text).toContain('n=28642')
  expect(text).toContain('wilson')
})

it('渲染理由、反证条件、持有周期、流动性、潜在回撤与仓位上限', async () => {
  await renderRisk({ risk: trusted })
  const text = host.textContent ?? ''
  expect(text).toContain('理由')
  expect(text).toContain('收盘站上布林上轨且量能放大')
  expect(text).toContain('反证条件')
  expect(text).toContain('收盘跌回中轨下方')
  expect(text).toContain('预计持有周期')
  expect(text).toContain('5 个交易日')
  expect(text).toContain('流动性')
  expect(text).toContain('中等（1~10 亿）')
  expect(text).toContain('潜在回撤')
  expect(text).toContain('-15.4%')
  expect(text).toContain('仓位上限')
  expect(text).toContain('5.9%')
  expect(text).toContain('风险预算 1.5% ÷ 潜在回撤 15.4%')
})

it('零样本时明确提示不要把 0 分/0% 当成结论', async () => {
  await renderRisk({ risk: noSamples })
  const text = host.textContent ?? ''
  expect(text).toContain('无样本')
  expect(text).toContain('不要把它读成')
  expect(text).toContain('csg_limit_up 不在历史帧中')
})

it('理由/反证条件为空时显示未填写而不是空白', async () => {
  await renderRisk({ risk: noSamples })
  expect(host.textContent).toContain('未填写')
})

it('loading 与 error 状态各有明确提示', async () => {
  await renderRisk({ loading: true })
  expect(host.textContent).toContain('正在计算信号统计')
  await renderRisk({ error: new Error('后端未启动') })
  const alert = host.querySelector('[role="alert"]')
  expect(alert?.textContent).toContain('后端未启动')
})

// --- 组合风险面板 ---------------------------------------------------------

it('默认不请求，点击后才评估组合风险', async () => {
  vi.mocked(api.portfolioRisk).mockResolvedValue(report)
  await renderPortfolio({ holdings: [
    { symbol: '600001.SH', weight: 0.6, drawdown_pct: 0.1 },
    { symbol: '600002.SH', weight: 0.4, drawdown_pct: 0.2 },
  ] })
  expect(vi.mocked(api.portfolioRisk)).not.toHaveBeenCalled()
  expect(host.textContent).toContain('2 笔持仓')
  await clickButton('评估组合风险')
  expect(vi.mocked(api.portfolioRisk)).toHaveBeenCalledTimes(1)
  const text = host.textContent ?? ''
  expect(text).toContain('行业集中度')
  expect(text).toContain('相关性')
  expect(text).toContain('单笔风险')
  expect(text).toContain('整体回撤预算')
})

it('展示行业集中度、有效独立注数与单笔风险表', async () => {
  vi.mocked(api.portfolioRisk).mockResolvedValue(report)
  await renderPortfolio({ holdings: [{ symbol: '600001.SH', weight: 1 }] })
  await clickButton('评估组合风险')
  const text = host.textContent ?? ''
  expect(text).toContain('银行')
  expect(text).toContain('60.0%')
  expect(text).toContain('有效行业数')
  expect(text).toContain('有效独立注数')
  expect(text).toContain('1.6')
  expect(text).toContain('600001.SH ↔ 600002.SH')
  expect(text).toContain('6.00%')
  expect(text).toContain('8.00%')
  expect(text).toContain('合计风险贡献')
  expect(text).toContain('14.00%')
})

it('组合面板批量查名称：表格与警告里代码旁都带上股票名称', async () => {
  vi.mocked(api.portfolioRisk).mockResolvedValue(report)
  await renderPortfolio({ holdings: [{ symbol: '600001.SH', weight: 1 }] })
  await clickButton('评估组合风险')
  // 名称按「代码集合」批量查：报告到达前按 holdings 查一次，到达后再按
  // 报告里的全部代码查一次（两跳是必要的，不是逐行请求）。至少有一跳把两只票一起带上了。
  expect(vi.mocked(api.instrumentNames).mock.calls.length).toBeGreaterThanOrEqual(1)
  expect(vi.mocked(api.instrumentNames).mock.calls.some(
    c => c[0].includes('600001.SH') && c[0].includes('600002.SH'),
  )).toBe(true)
  const text = host.textContent ?? ''
  expect(text).toContain('浦发银行')
  expect(text).toContain('齐鲁石化')
  // 后端拼好的整句警告也被就地补上名称
  expect(text).toContain('浦发银行(600001.SH) 与 齐鲁石化(600002.SH) 相关系数')
  // 表格里仍保留代码，方便核对
  expect(text).toContain('(600001.SH)')
})

it('金额带「元」单位，情景表解释负号与白话口径', async () => {
  vi.mocked(api.portfolioRiskByAccount).mockResolvedValue(accountReport)
  await renderPortfolio({ account: 'arena_test', equity: 1000000, cash: 28791.46, autoRun: true })
  const text = host.textContent ?? ''
  expect(text).toContain('万元')
  expect(text).toContain('少掉 6 个百分点')
  expect(text).toContain('负号=变少')
  expect(text).toContain('沪深300(000300.SH)')
  // 该 fixture 的 limitations 为空，折叠块本就不渲染；白话说明在情景块里已断言
  expect(text).toContain('线性估算')
})

it('口径与限制折叠块有白话标题，里面的代码也补名称', async () => {
  vi.mocked(api.portfolioRisk).mockResolvedValue({
    ...report,
    limitations: ['600001.SH 缺 60 日净值，波动率按较短样本估算'],
  })
  await renderPortfolio({ holdings: [{ symbol: '600001.SH', weight: 1 }] })
  await clickButton('评估组合风险')
  const text = host.textContent ?? ''
  expect(text).toContain('哪些数据没用上、结论打了折')
  expect(text).toContain('浦发银行(600001.SH) 缺')
})

it('超预算时给出可执行的削减建议', async () => {
  vi.mocked(api.portfolioRisk).mockResolvedValue(overBudget)
  await renderPortfolio({ holdings: [{ symbol: '600001.SH', weight: 1 }] })
  await clickButton('评估组合风险')
  const text = host.textContent ?? ''
  expect(text).toContain('超预算，建议削减')
  expect(text).toContain('600002.SH')
  expect(text).toContain('超预算 15.00%')
})

it('明确声明压力回撤不是预测也不保证不亏', async () => {
  vi.mocked(api.portfolioRisk).mockResolvedValue(report)
  await renderPortfolio({ holdings: [{ symbol: '600001.SH', weight: 1 }] })
  await clickButton('评估组合风险')
  const text = host.textContent ?? ''
  expect(text).toContain('不是预测')
  expect(text).toContain('不保证不亏')
})

it('空持仓时直接说明无法评估', async () => {
  await renderPortfolio({ holdings: [] })
  expect(host.textContent).toContain('没有持仓')
  expect(vi.mocked(api.portfolioRisk)).not.toHaveBeenCalled()
})

it('相关性无定义时显示无定义而不是 0', async () => {
  vi.mocked(api.portfolioRisk).mockResolvedValue({
    ...report,
    correlation: {
      ...report.correlation, effective_bets: null, average_correlation: null, highest_pair: null,
      warnings: ['组合方差相对量 ≤ 0：权重组合近似完全对冲'],
    },
  })
  await renderPortfolio({ holdings: [{ symbol: '600001.SH', weight: 1 }] })
  await clickButton('评估组合风险')
  const text = host.textContent ?? ''
  expect(text).toContain('无定义')
  expect(text).toContain('近似完全对冲')
})

// --- 账户口径：仓位 / 主题重复 / 情景冲击 --------------------------------

const accountReport: PortfolioRiskReport = {
  ...report,
  basis: 'actual_positions',
  account_name: '主账户',
  account_equity: 1000000,
  account_cash: 28791.46,
  account_market_value: 971208.55,
  total_pnl: 12031.2,
  cash_ratio: 0.0288,
  invested_ratio: 0.9712,
  max_drawdown: 6.67,
  current_drawdown: 6.67,
  benchmark: {
    index: '000300.SH', days: 3,
    account_return_pct: 0.89, benchmark_return_pct: -0.94, excess_pct: 1.83,
  },
  volatility: { '600001.SH': 41.95, '600002.SH': 81.99 },
  concept: {
    groups: [
      {
        concept: '机器人概念', symbols: ['600001.SH', '600002.SH'],
        symbol_count: 2, weight: 0.3903, members: ['人形机器人', '减速器'],
      },
      { concept: '新能源汽车', symbols: ['600001.SH'], symbol_count: 1, weight: 0.2903, members: [] },
    ],
    clusters: [
      { label: '机器人概念', members: ['人形机器人', '减速器', '机器人概念'] },
    ],
    overlaps: [
      {
        tag: 'AI视频',
        candidates: [
          { parent: 'AIGC概念', containment_pct: 88.0 },
          { parent: '人工智能', containment_pct: 85.3 },
        ],
        reason: '同时高度落在多个概念里，归属歧义，未归并',
      },
    ],
    excluded_generic: [
      { concept: '融资融券', coverage_pct: 69.8 },
      { concept: '深股通', coverage_pct: 34.0 },
    ],
    concept_count: 2,
    warnings: ['2 只持仓共享「机器人概念」（含 人形机器人、减速器），合计权重 39.0% —— 它们很可能受同一个风险驱动'],
    limitations: [],
  },
  scenario: {
    shocks: [-0.1, -0.2],
    positions: [
      {
        symbol: '600001.SH', weight: 0.6,
        equity_impact_pct_10: -6.0, equity_impact_pct_20: -12.0,
        amount_10: -60000, amount_20: -120000,
      },
      {
        symbol: '600002.SH', weight: 0.4,
        equity_impact_pct_10: -4.0, equity_impact_pct_20: -8.0,
        amount_10: -40000, amount_20: -80000,
      },
    ],
    total_equity_impact_pct_10: -10.0,
    total_equity_impact_pct_20: -20.0,
    total_amount_10: -100000,
    total_amount_20: -200000,
    note: '线性近似：假设其余持仓不动，不含相关性放大',
  },
}

it('账户口径下自动评估，并显示现金比例、最大回撤与基准对比', async () => {
  vi.mocked(api.portfolioRiskByAccount).mockResolvedValue(accountReport)
  await renderPortfolio({ account: 'arena_test', equity: 1000000, cash: 28791.46, autoRun: true })
  expect(vi.mocked(api.portfolioRiskByAccount)).toHaveBeenCalledTimes(1)
  expect(vi.mocked(api.portfolioRisk)).not.toHaveBeenCalled()
  const text = host.textContent ?? ''
  expect(text).toContain('账户权益')
  expect(text).toContain('现金')
  expect(text).toContain('2.9%')
  expect(text).toContain('账户最大回撤')
  expect(text).toContain('6.67%')
  expect(text).toContain('000300.SH')
  expect(text).toContain('超额')
  // 现金 <5% 必须给出缓冲不足的警告
  expect(text).toContain('几乎没有缓冲')
})

it('现金充足时不误报缓冲不足', async () => {
  vi.mocked(api.portfolioRiskByAccount).mockResolvedValue({
    ...accountReport, cash_ratio: 0.35, account_cash: 350000,
  })
  await renderPortfolio({ account: 'arena_test', autoRun: true })
  expect(host.textContent).not.toContain('几乎没有缓冲')
})

it('展示主题重复暴露，并说明通用标签被剔除的原因', async () => {
  vi.mocked(api.portfolioRiskByAccount).mockResolvedValue(accountReport)
  await renderPortfolio({ account: 'arena_test', autoRun: true })
  const text = host.textContent ?? ''
  expect(text).toContain('主题重复')
  expect(text).toContain('机器人概念')
  expect(text).toContain('×2')
  expect(text).toContain('39.0%')
  // 归并来的原始标签必须写出来，否则用户不知道「机器人概念 2 只」是怎么凑的
  expect(text).toContain('含 人形机器人、减速器')
  expect(text).toContain('同一个风险驱动')
  expect(text).toContain('通用标签')
  expect(text).toContain('融资融券')
})

it('展示近义标签归并的依据，并明确它不是语义理解', async () => {
  vi.mocked(api.portfolioRiskByAccount).mockResolvedValue(accountReport)
  await renderPortfolio({ account: 'arena_test', autoRun: true })
  const text = host.textContent ?? ''
  expect(text).toContain('近义标签已归并')
  expect(text).toContain('按标的包含度')
  expect(text).toContain('不是语义理解')
  expect(text).toContain('人形机器人')
})

it('归属歧义的标签不强行归并，而是列出来说明差别太小', async () => {
  vi.mocked(api.portfolioRiskByAccount).mockResolvedValue(accountReport)
  await renderPortfolio({ account: 'arena_test', autoRun: true })
  const text = host.textContent ?? ''
  expect(text).toContain('横跨多个概念，未强行归并')
  expect(text).toContain('AI视频')
  expect(text).toContain('AIGC概念 88%')
  expect(text).toContain('人工智能 85%')
  expect(text).toContain('并到任一边都会误导')
})

it('显示跌 10% / 20% 对账户的影响（百分点与金额）', async () => {
  vi.mocked(api.portfolioRiskByAccount).mockResolvedValue(accountReport)
  await renderPortfolio({ account: 'arena_test', autoRun: true })
  const text = host.textContent ?? ''
  expect(text).toContain('跌 10% / 20% 会怎样')
  expect(text).toContain('-6.00pp')
  expect(text).toContain('-12.00pp')
  expect(text).toContain('全部持仓跌10%')
  expect(text).toContain('-10.00pp')
  expect(text).toContain('线性近似')
})

it('波动率超过 60% 的标的被标出', async () => {
  vi.mocked(api.portfolioRiskByAccount).mockResolvedValue(accountReport)
  await renderPortfolio({ account: 'arena_test', autoRun: true })
  const row = [...host.querySelectorAll('tr')].find(r => r.textContent?.includes('600002.SH'))
  expect(row?.textContent).toContain('82%')
  expect(row?.querySelector('.text-danger')).toBeTruthy()
})

it('观察口径必须声明这不是实际持仓', async () => {
  vi.mocked(api.portfolioRisk).mockResolvedValue(accountReport)
  await renderPortfolio({
    holdings: [{ symbol: '600001.SH', weight: 1 }],
    warnObservation: true,
  })
  await clickButton('评估组合风险')
  expect(host.textContent).toContain('它不是你的实际持仓')
})

// --- 手工录入真仓口径 -----------------------------------------------------

const manualReport: PortfolioRiskReport = {
  ...accountReport,
  basis: 'manual_positions',
  account_name: null,
  max_drawdown: null,
  current_drawdown: null,
  benchmark: null,
  nav_limitation: '手工录入的持仓没有历史净值序列，账户级回撤与基准对比无法计算',
}

it('手工口径只发 source=manual 请求，不碰模拟盘账户与等权 POST', async () => {
  vi.mocked(api.portfolioRiskByManual).mockResolvedValue(manualReport)
  await renderPortfolio({ manual: true, autoRun: true })
  expect(vi.mocked(api.portfolioRiskByManual)).toHaveBeenCalledTimes(1)
  expect(vi.mocked(api.portfolioRiskByAccount)).not.toHaveBeenCalled()
  expect(vi.mocked(api.portfolioRisk)).not.toHaveBeenCalled()
  const text = host.textContent ?? ''
  expect(text).toContain('真实持仓（手工录入）')
  expect(text).toContain('账户概览')
  expect(text).toContain('没有历史净值序列')
  expect(text).toContain('—')
})

it('account 与 manual 同时给时以账户为准，不发手工口径请求', async () => {
  vi.mocked(api.portfolioRiskByAccount).mockResolvedValue(accountReport)
  await renderPortfolio({ account: 'arena_test', manual: true, autoRun: true })
  expect(vi.mocked(api.portfolioRiskByAccount)).toHaveBeenCalledTimes(1)
  expect(vi.mocked(api.portfolioRiskByManual)).not.toHaveBeenCalled()
})

// --- 买入前仓位模拟 -------------------------------------------------------

const simReport: BuySimulationReport = {
  new_symbol: '600941.SH',
  new_weight_pct: 10,
  cash_weight_before_pct: 2.88,
  over_cash: true,
  equity: 1000000,
  before: {
    top_industry: '银行', top_industry_share: 0.6, effective_industries: 1.92,
    top_concept: '减速器', top_concept_symbols: 2, top_concept_weight: 0.39,
    effective_bets: 1.6, max_position_weight: 0.6,
    stressed_drawdown_pct: 16.5895, budget_usage_pct: 110.6, over_budget: true,
  },
  after: {
    top_industry: '银行', top_industry_share: 0.55, effective_industries: 2.1,
    top_concept: '减速器', top_concept_symbols: 2, top_concept_weight: 0.35,
    effective_bets: 1.8, max_position_weight: 0.55,
    stressed_drawdown_pct: 17.1585, budget_usage_pct: 114.4, over_budget: true,
  },
  deltas: {
    top_industry_share: -0.05, effective_industries: 0.18, effective_bets: 0.2,
    max_position_weight: -0.05, stressed_drawdown_pct: 0.569, budget_usage_pct: 3.8,
  },
  worst_case_loss: -171585,
  limitations: ['买入后该票占权益 10.0%，超过可用现金占比 2.9% —— 差额必须来自卖出其它持仓'],
  warnings: [],
}

async function renderBuySim(props: Parameters<typeof BuySimulationCard>[0]) {
  await act(async () => root.render(
    <QueryClientProvider client={client}><BuySimulationCard {...props} /></QueryClientProvider>,
  ))
  await settle()
}

it('买入模拟默认不请求，点「模拟」后才算', async () => {
  vi.mocked(api.buySimulation).mockResolvedValue(simReport)
  await renderBuySim({ account: 'arena_test', equity: 1000000, symbol: '600941.SH' })
  expect(vi.mocked(api.buySimulation)).not.toHaveBeenCalled()
  await clickButton('模拟')
  expect(vi.mocked(api.buySimulation)).toHaveBeenCalledTimes(1)
  const body = vi.mocked(api.buySimulation).mock.calls[0][0]
  expect(body.symbol).toBe('600941.SH')
  expect(body.new_weight_pct).toBe(5)
  expect(body.account).toBe('arena_test')
})

it('模拟结果给买入前后的对照与最坏情境亏损', async () => {
  vi.mocked(api.buySimulation).mockResolvedValue(simReport)
  await renderBuySim({ account: 'arena_test', equity: 1000000, symbol: '600941.SH' })
  await clickButton('模拟')
  const text = host.textContent ?? ''
  expect(text).toContain('压力回撤')
  expect(text).toContain('16.59%')
  expect(text).toContain('17.16%')
  expect(text).toContain('有效独立注数')
  expect(text).toContain('最坏情境亏损')
  expect(text).toContain('17.16 万')
  expect(text).toContain('不是预测')
})

it('超过现金占比时明确指出差额要靠卖出', async () => {
  vi.mocked(api.buySimulation).mockResolvedValue(simReport)
  await renderBuySim({ account: 'arena_test', equity: 1000000, symbol: '600941.SH' })
  await clickButton('模拟')
  const text = host.textContent ?? ''
  expect(text).toContain('超过当前现金占比')
  expect(text).toContain('本模拟不替你决定卖哪只')
  // 模拟结果里也给标的补名称，不再只有一串代码
  expect(text).toContain('中国移动(600941.SH)')
  expect(text).toContain('本次模拟')
})

it('买入后集中度变差时用红色标出', async () => {
  vi.mocked(api.buySimulation).mockResolvedValue(simReport)
  await renderBuySim({ account: 'arena_test', equity: 1000000, symbol: '600941.SH' })
  await clickButton('模拟')
  const row = [...host.querySelectorAll('tr')].find(r => r.textContent?.includes('压力回撤'))
  expect(row?.textContent).toContain('+0.57')
  expect(row?.querySelector('.text-danger')).toBeTruthy()
})

it('权重非法时不发请求', async () => {
  vi.mocked(api.buySimulation).mockResolvedValue(simReport)
  await renderBuySim({ account: 'arena_test', equity: 1000000, symbol: '600941.SH' })
  const input = host.querySelector('input[aria-label="买入后占账户权益百分比"]') as HTMLInputElement
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
    setter.call(input, '150')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await settle()
  await clickButton('模拟')
  expect(vi.mocked(api.buySimulation)).not.toHaveBeenCalled()
  expect(host.textContent).toContain('权重需在 0~100 之间')
})

it('手工口径的买入模拟带 source=manual，不带 account', async () => {
  vi.mocked(api.buySimulation).mockResolvedValue(simReport)
  await renderBuySim({ manual: true, symbol: '600941.SH' })
  await clickButton('模拟')
  expect(vi.mocked(api.buySimulation)).toHaveBeenCalledTimes(1)
  const body = vi.mocked(api.buySimulation).mock.calls[0][0]
  expect(body.source).toBe('manual')
  expect(body.account).toBeUndefined()
})

// --- 「专家增强」6 个新键：数据质量/可执行性/尾部/费用/基准/因子 -------------

it('把后端 6 个增强键渲染成普通投资者看得懂的六块，缺数据时也不白屏', async () => {
  const enriched: PortfolioRiskReport = {
    ...report,
    data_quality: {
      as_of: '2026-11-14', data_through: '2026-11-13', common_days: 6,
      verdict: 'reference', flags: ['「600001.SH 近 22 个交易日缺 3 根 K 线」'],
      stale: false, insufficient: true,
      limitations: ['相关性仅按 6 个共同交易日计算, 低于 20 日门槛'],
    },
    actionability: {
      suggested_cuts: [
        { symbol: '600002.SH', current_weight: 0.4, reduce_by: 0.4, new_weight: 0,
          executable_today: false, executable_reduce_by: null, actionability: '今日跌停封板，顺延' },
      ],
      market_state_symbols: ['600002.SH'], note: 'reduce_by 是风险目标；executable_reduce_by 才是今日能减的量。',
    },
    tail_scenarios: {
      correlation_amplified: { amplification_factor: 1.27, amplified_loss_10pct: -12.7, base_loss_10pct: -10 },
      historical_stress: [{ label: '2015-06 去杠杆', equity_drawdown_pct: -32.0 }],
      limitations: ['固定字面重放系数，不联网'],
    },
    fee_passthrough: {
      sell_cost_pct: 0.00126, market_value_estimate: 1000000,
      net_after_fees_estimate: 998740, note: '按 fees.py 卖出综合费率', limitations: [],
    },
    relative_performance: {
      period_return: 0.05, benchmark_return: 0.03, excess_return: 0.02,
      beta: 1.1, alpha: 0.01, information_ratio: 0.8, common_days: 40,
      limitations: ['样本不足, 指标仅供参考'],
    },
    factor_attribution: {
      exposures: {}, coverage: { covered: 0, total: 2 },
      limitations: ['无个股残差收益序列, 无法拆出特异性收益'],
    },
  }
  vi.mocked(api.portfolioRisk).mockResolvedValue(enriched)
  await renderPortfolio({ holdings: [
    { symbol: '600001.SH', weight: 0.6, drawdown_pct: 0.1 },
    { symbol: '600002.SH', weight: 0.4, drawdown_pct: 0.2 },
  ] })
  await clickButton('评估组合风险')
  const text = host.textContent ?? ''
  // P0-1 数据质量门禁
  expect(text).toContain('数据新鲜度与覆盖')
  expect(text).toContain('2026-11-13')
  expect(text).toContain('仅供参考')
  expect(text).toContain('缺 3 根 K 线')
  // P0-3 可执行性 + 尾部
  expect(text).toContain('今天实际能减多少')
  expect(text).toContain('顺延/待核实')
  expect(text).toContain('齐鲁石化(600002.SH)')  // Sym 补名称
  expect(text).toContain('极端行情会怎样')
  expect(text).toContain('1.27×')
  expect(text).toContain('2015-06 去杠杆')
  // P2-8 费用穿透
  expect(text).toContain('到手金额')
  expect(text).toContain('0.126%')
  expect(text).toContain('全部变现到手')
  // P1-5 相对基准 + 因子
  expect(text).toContain('相对基准')
  expect(text).toContain('超额收益')
  expect(text).toContain('β（跟涨跌幅）')
  expect(text).toContain('因子风险归因')
  expect(text).toContain('0/2')
})

it('老报告没有 6 个增强键时，原结论照常显示、不新增空白块', async () => {
  vi.mocked(api.portfolioRisk).mockResolvedValue(report)
  await renderPortfolio({ holdings: [
    { symbol: '600001.SH', weight: 0.6, drawdown_pct: 0.1 },
    { symbol: '600002.SH', weight: 0.4, drawdown_pct: 0.2 },
  ] })
  await clickButton('评估组合风险')
  const text = host.textContent ?? ''
  expect(text).toContain('行业集中度')  // 旧块还在
  expect(text).toContain('整体回撤预算')
  expect(text).not.toContain('数据新鲜度与覆盖')  // 新块不硬塞
  expect(text).not.toContain('到手金额（扣费后）')
})
