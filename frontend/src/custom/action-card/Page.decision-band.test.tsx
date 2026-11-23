// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ActionCard } from './Page'

const mocks = vi.hoisted(() => ({
  actionCard: vi.fn(),
  manualHoldings: vi.fn(),
  positionPlanSave: vi.fn(),
  positionPlanDelete: vi.fn(),
}))

vi.mock('@/lib/api', () => ({ api: mocks }))

let host: HTMLDivElement
let root: Root

/** 用 000333.SZ 的真实响应做夹具 —— 这组数字正是「只看第一行胜率会读反」的样本。 */
function realCard() {
  return {
    symbol: '000333.SZ',
    name: '美的集团',
    state: 'holding',
    state_label: '实际持仓',
    as_of: '2026-11-16',
    sections: { facts: '现在的情况', inference: '系统的推测', user_plan: '你的打算' },
    questions: {},
    facts: {
      price: 91.32,
      date: '2026-11-16',
      change_pct: 0.85,
      open: 90.6,
      high: 91.8,
      low: 90.2,
      close_position_pct: 78,
      body_ratio_pct: 50.3,
      volume: 1_234_567,
      volume_ratio_pct: 112,
      mas: { MA10: 89.04, MA20: 87.56, MA60: 85.68 },
      ma_deviation_pct: { MA10: 2.5, MA20: 4.3, MA60: 6.6 },
      high_250: 91.32,
      low_250: 68.61,
      position_in_250_pct: 100,
      rsi14: 63.2,
      macd_hist: 0.47,
      boll: { upper: 91.55, mid: 87.56, lower: 83.57 },
      atr14: 2.06,
      atr14_pct: 2.26,
      vol_20d_annual_pct: 21.2,
      vol_60d_annual_pct: 20.9,
      key_levels: {
        high_20: { price: 91.86, distance_pct: -0.59 },
        low_20: { price: 83.0, distance_pct: 10.02 },
        high_60: { price: 91.86, distance_pct: -0.59 },
        low_60: { price: 78.3, distance_pct: 16.63 },
      },
      chip_densest_above: null,
      chip_densest_below: { low: 84.77, high: 85.71, pct: 16.5, total_pct: 100.1 },
      holding: {
        qty: 200,
        avg_cost: 88.1,
        market_value: 18264,
        weight: 0.12,
        pnl: 644,
        pnl_pct: 0.0365,
        entry_date: '2026-10-17',
        holding_days: 30,
      },
    },
    inference: {
      strength_stance: 'strong',
      strength_label: '偏强',
      bullish_evidence: ['MA20 上穿 MA60'],
      bearish_evidence: [],
      neutral_evidence: [],
      analog_pattern: '放量收在当日高位',
      analog_sample_size: 48,
      // 1 日强于基准、3/5/10 日全部弱于基准且平均收益为负 —— 只看第一行的经典陷阱
      forward_stats: {
        '1': { win_rate: 67, mean_pct: 0.16, median_pct: 0.28, baseline_win_rate: 51, baseline_mean_pct: 0.1, edge_vs_baseline_pp: 16 },
        '3': { win_rate: 33, mean_pct: -0.91, median_pct: -0.62, baseline_win_rate: 55, baseline_mean_pct: 0.27, edge_vs_baseline_pp: -22 },
        '5': { win_rate: 33, mean_pct: -0.59, median_pct: -0.71, baseline_win_rate: 58, baseline_mean_pct: 0.46, edge_vs_baseline_pp: -25 },
        '10': { win_rate: 33, mean_pct: -1.38, median_pct: -1.44, baseline_win_rate: 60, baseline_mean_pct: 0.86, edge_vs_baseline_pp: -27 },
      },
      baseline_stats: {},
      confidence: 'low' as const,
      confidence_basis: '样本 48 例',
      caveats: [],
      rule_validation: null,
    },
    user_plan: {
      state_note: '实际持仓',
      counts_toward_pnl: true,
      planned_hold_days: 60,
      acceptable_loss_pct: 8,
      batches: [{ price: 86.5, amount: 5000, label: '加一批', done: false }],
      note: '',
      progress: null,
    },
    strength: {
      stance: 'strong' as const,
      label: '偏强',
      bullish_evidence: ['MA20 上穿 MA60'],
      bearish_evidence: [],
      neutral_evidence: [],
      note: '',
    },
    invalidation: [
      { condition: '尚未设置可接受亏损，无法给出账面止损线', price: null, meaning: '', source: '计划', severity: 'warning' as const },
      { condition: '跌破 MA10（89.04）', price: 89.04, meaning: '短线转弱', source: '均线', severity: 'warning' as const },
      { condition: '跌破最近 20 日低点 83.00', price: 83.0, meaning: '原判断作废', source: '形态', severity: 'critical' as const },
    ],
    entry_conditions: [
      { condition: '放量突破近 20 日高点 91.86', price: 91.86, rationale: '确认走强', kind: 'breakout' as const },
      { condition: '回踩 MA60（85.68）不破可分批', price: 85.68, rationale: '低吸', kind: 'pullback' as const },
    ],
    checks: [
      { item: '营收同比 1.19%', why: '看成长', where: '财报', status: 'checked' as const },
      { item: 'ROE 18.06%', why: '看赚钱能力', where: '财报', status: 'checked' as const },
      { item: '所属行业景气度', why: '看外部环境', where: '行业数据', status: 'unverified' as const },
      { item: '是否有未披露重大事项', why: '避雷', where: '公告', status: 'unverified' as const },
    ],
    fundamentals: {
      available: true,
      latest_period: '2026Q3',
      latest_announce_date: '2026-10-28',
      metrics: [
        { label: 'ROE', key: 'roe', value: 18.06, unit: '%' },
        { label: '营收同比', key: 'revenue_yoy', value: 1.19, unit: '%' },
        { label: '净利同比', key: 'net_income_yoy', value: 9.27, unit: '%' },
        { label: '资产负债率', key: 'debt_to_asset_ratio', value: 62.42, unit: '%' },
        { label: '毛利率', key: 'gross_margin', value: 26.1, unit: '%' },
        { label: '经营现金流', key: 'ocf', value: 412.5, unit: '亿' },
        { label: '存货周转', key: 'inv_turn', value: 5.2, unit: '次' },
        { label: '应收周转', key: 'ar_turn', value: 7.8, unit: '次' },
        { label: '研发占比', key: 'rd_ratio', value: 3.4, unit: '%' },
        { label: '分红率', key: 'payout', value: 61.2, unit: '%' },
        { label: '质押比例', key: 'pledge', value: 0, unit: '%' },
      ],
      revenue_trend: [
        { period: '2026Q2', announced: '2026-08-30', value: 3.1 },
        { period: '2026Q3', announced: '2026-10-28', value: 1.19 },
      ],
      profit_trend: [],
      announcements: [],
      snapshot_sources: [],
      unavailable: [],
      limitations: [],
      note: '',
    },
    reconciliation: null,
    caveats: ['不构成投资建议'],
  }
}

async function mount() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={new QueryClient({
        defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
      })}>
        <MemoryRouter initialEntries={['/action-card?symbol=000333.SZ']}>
          <ActionCard />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  // 等 actionCard query 落地：价格地图是聚合层里最先出现的块，用它当就绪信号。
  // 轮询必须包在 act 里，否则 React 会为查询落地后的状态更新报 act 警告。
  for (let i = 0; i < 100; i += 1) {
    if ((host.textContent ?? '').includes('价格地图')) return
    await act(async () => { await new Promise(r => setTimeout(r, 10)) })
  }
  throw new Error('行动卡数据尚未渲染')
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  mocks.actionCard.mockResolvedValue(realCard())
  mocks.manualHoldings.mockResolvedValue({ holdings: [], count: 0, positions: [], limitations: [] })
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.clearAllMocks()
})

it('首屏结论带把现价/涨跌/持仓浮盈亏提到最上面, 不再埋在事实段网格里', async () => {
  await mount()
  const text = host.textContent ?? ''
  expect(text).toContain('美的集团')
  expect(text).toContain('91.32')
  expect(text).toContain('+0.85%')
  expect(text).toContain('持 200 股')
  expect(text).toContain('成本 88.10')
  expect(text).toContain('浮盈亏 644 元（3.65%）')
})

it('价格地图把前高/布林/筹码/失效价/买点/计划批次合成一根按价格降序的轴', async () => {
  await mount()
  const text = host.textContent ?? ''
  expect(text).toContain('价格地图')
  // 同价合并：91.86 既是 20 日高也是 60 日高, 还挂了一条买点
  expect(text).toContain('20日高 · 60日高 · 考虑买入：放量突破近 20 日高点 91.86')
  // 按价格降序：91.86 在 91.55（布林上轨）之前，91.55 在 85.68（MA60 回踩买点）之前
  expect(text.indexOf('91.86')).toBeLessThan(text.indexOf('布林上轨'))
  expect(text.indexOf('布林上轨')).toBeLessThan(text.indexOf('回踩 MA60'))
  // 83.00 同时是 20 日低和 critical 失效价 -> 归为失效级别
  expect(text).toContain('失效条件：跌破最近 20 日低点 83.00')
  expect(text).toContain('筹码密集区 84.77~85.71（占 16.5%）')
  expect(text).toContain('计划第 1 批买入')
  // 现价行带一年分位, 这是原来完全没渲染的关键事实
  expect(text).toContain('现价')
  expect(text).toContain('近一年 100% 分位')
  // 距离百分比：91.86 比现价 91.32 高 0.59%
  expect(text).toContain('+0.59%')
})

it('前瞻表补齐平均涨跌与相对基准, 并自动点破「1 日偏强、3 日以上全弱」的读反陷阱', async () => {
  await mount()
  const text = host.textContent ?? ''
  expect(text).toContain('平均涨跌')
  expect(text).toContain('对基准')
  expect(text).toContain('67%')
  expect(text).toContain('vs 51%')
  // 原来完全没渲染的 mean_pct
  expect(text).toContain('-0.91%')
  expect(text).toContain('-1.38%')
  expect(text).toContain('-22pp')
  expect(text).toContain('-27pp')
  // 自动归纳必须点明「别拿 1 日胜率做长线决定」
  expect(text).toContain('只有最短的 1 日强于基准')
  expect(text).toContain('3 日、5 日、10 日全部弱于基准、平均收益还是负的')
  expect(text).toContain('别拿 1 日的胜率去做长线决定')
})

it('波动换算成钱和区间, 普通投资者不用理解 ATR 这个缩写', async () => {
  await mount()
  const text = host.textContent ?? ''
  expect(text).toContain('一天大概晃')
  expect(text).toContain('±2.26%')
  expect(text).toContain('100 股约')
  expect(text).toContain('±206 元')
  expect(text).toContain('1 万元仓位约')
  expect(text).toContain('±226 元')
  // 21.2 / sqrt(12) = 6.12 -> ±6.1%
  expect(text).toContain('一个月大概率在')
  expect(text).toContain('±6.1%')
})

it('核对清单按 checked/unverified 分三态, 不再把已核对的显示成待办方框', async () => {
  await mount()
  const text = host.textContent ?? ''
  expect(text).toContain('✓ 营收同比 1.19%')
  expect(text).toContain('✓ ROE 18.06%')
  expect(text).toContain('☐ 所属行业景气度')
  expect(text).toContain('☐ 是否有未披露重大事项')
  expect(text).toContain('待你核对')
  // 已核对的两条不能带「待你核对」
  expect(text).not.toContain('✓ 营收同比 1.19% — 看成长（财报 · 待你核对）')
})

it('财务指标只摊开 4 个核心, 其余折叠可展开', async () => {
  await mount()
  const text = host.textContent ?? ''
  expect(text).toContain('展开其余 7 项财务指标')
  // 核心 4 项直接可见
  expect(text).toContain('ROE')
  expect(text).toContain('资产负债率')
  // 折叠项默认不在 DOM 里
  expect(text).not.toContain('研发占比')
})
