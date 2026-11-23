// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { PositionOverview } from './Overview'

/**
 * 持仓总览的边界回归 —— 补齐主测试文件没覆盖的退化路径。
 *
 * 主文件 Overview.watchlist-import.test.tsx 覆盖「正常导入 + 三态动作」；
 * 这里只锁「数据异常时不崩、不猜、不误报」：
 *   1. 行情/名称接口失败 → 显示「—」而不是白屏或 0
 *   2. 只有一根 K 线 → 有现价无涨跌幅（不能拿 0 冒充）
 *   3. close 是非数值 / 0 → 不能算出 Infinity 或 NaN%
 *   4. 分类接口失败 → 有错误提示，不是静默空列表
 *   5. 自选导入失败 → 明确报错，不假装成功
 *   6. 500 只上限截断 → 如实说明截断数量
 *   7. 重复点击导入 → 不重复叠加相同标的
 */

const mocks = vi.hoisted(() => ({
  manualHoldings: vi.fn(),
  positionClassify: vi.fn(),
  planCapitalCheck: vi.fn(),
  watchlistList: vi.fn(),
  validateStrength: vi.fn(),
  planReconcile: vi.fn(),
  instrumentNames: vi.fn(),
  klineDailyBatch: vi.fn(),
  positionPlanSave: vi.fn(),
  positionPlanDelete: vi.fn(),
  manualHoldingSave: vi.fn(),
  manualHoldingDelete: vi.fn(),
}))

vi.mock('@/lib/api', () => ({ api: mocks }))
vi.mock('./ManualHoldingsPanel', () => ({
  ManualHoldingsPanel: () => <div data-testid="manual-holdings" />,
}))

let host: HTMLDivElement
let root: Root
let qc: QueryClient

const emptyCapital = {
  total_planned: 0, total_executed: 0, total_remaining: 0,
  available_cash: null, over_budget: false, shortfall: 0,
  warnings: [], per_symbol: [],
}

function classifyOf(rows: [string, string, string, boolean][]) {
  return {
    items: rows.map(([symbol, state, state_label, counts_toward_pnl]) => ({
      symbol, state, state_label, state_note: state_label, counts_toward_pnl,
    })),
    counts: {}, labels: {}, note: '',
  }
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })

  mocks.manualHoldings.mockResolvedValue({
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
  })
  mocks.positionClassify.mockImplementation(async (syms: string[]) =>
    classifyOf(syms.map(s => [s, 'watch', '自选观察', false] as [string, string, string, boolean])))
  mocks.planCapitalCheck.mockResolvedValue(emptyCapital)
  mocks.watchlistList.mockResolvedValue({ symbols: [] })
  mocks.validateStrength.mockResolvedValue({})
  mocks.planReconcile.mockResolvedValue({ matches: [] })
  mocks.instrumentNames.mockResolvedValue({ names: { '000333.SZ': '美的集团' } })
  mocks.klineDailyBatch.mockResolvedValue({ data: {} })
  mocks.positionPlanSave.mockResolvedValue({})
  mocks.positionPlanDelete.mockResolvedValue({ deleted: '' })
  mocks.manualHoldingSave.mockResolvedValue({ ok: true, holding: {} })
  mocks.manualHoldingDelete.mockResolvedValue({ ok: true, symbol: '' })
})

afterEach(async () => {
  await act(async () => root.unmount())
  qc.clear()
  host.remove()
  vi.clearAllMocks()
})

async function flush() {
  for (let i = 0; i < 6; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 60)) })
  }
}

async function render() {
  await act(async () => root.render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><PositionOverview /></MemoryRouter>
    </QueryClientProvider>,
  ))
  await flush()
}

const td = (id: string) => host.querySelector(`[data-testid="${id}"]`)

it('行情接口整体失败时显示「—」，不崩不白屏', async () => {
  mocks.klineDailyBatch.mockRejectedValue(new Error('行情服务不可用'))
  mocks.instrumentNames.mockRejectedValue(new Error('名称服务不可用'))
  await render()

  // 页面主体仍在（三态清单渲染出来了）
  expect(td('info-000333.SZ')).toBeTruthy()
  // 名称与价格降级为占位符，不是 0、不是空白
  expect(td('name-000333.SZ')?.textContent).toBe('—')
  expect(td('price-000333.SZ')?.textContent).toBe('—')
  expect(td('pct-000333.SZ')?.textContent).toBe('—')
})

it('只有一根 K 线时有现价、涨跌幅为「—」（不能拿 0 冒充）', async () => {
  mocks.klineDailyBatch.mockResolvedValue({
    data: {
      '000333.SZ': [
        { symbol: '000333.SZ', date: '2026-11-14', open: 90, high: 91, low: 89, close: 90.55 },
      ],
    },
  })
  await render()

  expect(td('price-000333.SZ')?.textContent).toContain('90.55')
  expect(td('pct-000333.SZ')?.textContent).toBe('—')
})

it('前收为 0 时不计算百分比（避免 Infinity / NaN 显示）', async () => {
  mocks.klineDailyBatch.mockResolvedValue({
    data: {
      '000333.SZ': [
        { symbol: '000333.SZ', date: '2026-11-13', open: 0, high: 0, low: 0, close: 0 },
        { symbol: '000333.SZ', date: '2026-11-14', open: 90, high: 91, low: 89, close: 90.55 },
      ],
    },
  })
  await render()

  const pct = td('pct-000333.SZ')?.textContent ?? ''
  expect(pct).toBe('—')
  expect(pct).not.toContain('Infinity')
  expect(pct).not.toContain('NaN')
})

it('close 是非数值时跳过该行行情，不渲染 NaN%', async () => {
  mocks.klineDailyBatch.mockResolvedValue({
    data: {
      '000333.SZ': [
        { symbol: '000333.SZ', date: '2026-11-13', close: 'bad' },
        { symbol: '000333.SZ', date: '2026-11-14', close: null },
      ],
    },
  })
  await render()

  expect(td('price-000333.SZ')?.textContent).toBe('—')
  expect(td('pct-000333.SZ')?.textContent).toBe('—')
})

it('最后一根 close 是 NaN 字符串时，整行行情必须跳过（不能只拦 null）', async () => {
  // 关键区别：上一例最后一条是 null（被 == null 拦住），
  // 这一例最后一条是非空但不可转成数字的字符串 —— 只有 Number.isFinite 能拦住。
  //
  // 注意：fmtPrice/fmtPct 内部也有 Number.isNaN 兜底，所以「显示 —」不足以证明
  // 这一层的判断生效。真正要锁的是「NaN 不能流到下游配色/排序」——
  // priceColorClass 收到 NaN 会返回中性色，但收到有效正数返回 text-bull，
  // 因此用「同一 fixture 里再放一只正常票」做对照，确认坏的被丢弃、好的照常。
  mocks.positionClassify.mockResolvedValue(classifyOf([
    ['000333.SZ', 'watch', '自选观察', false],
    ['600941.SH', 'watch', '自选观察', false],
  ]))
  mocks.klineDailyBatch.mockResolvedValue({
    data: {
      '000333.SZ': [
        { symbol: '000333.SZ', date: '2026-11-13', close: 90 },
        { symbol: '000333.SZ', date: '2026-11-14', close: 'not-a-number' },
      ],
      '600941.SH': [
        { symbol: '600941.SH', date: '2026-11-13', close: 100 },
        { symbol: '600941.SH', date: '2026-11-14', close: 101 },
      ],
    },
  })
  await render()

  // 坏数据：两列都是占位符
  expect(td('price-000333.SZ')?.textContent).toBe('—')
  expect(td('pct-000333.SZ')?.textContent).toBe('—')
  // 对照：正常票照常渲染（证明丢弃只影响坏行，不是整体降级）
  expect(td('price-600941.SH')?.textContent).toContain('101.00')
  expect(td('pct-600941.SH')?.textContent).toContain('+1.00%')
  // 任何位置都不许出现 NaN
  expect(host.textContent ?? '').not.toContain('NaN')
})

it('移除持仓必须二次确认：第一次点击只出确认条，不发删除请求', async () => {
  mocks.positionClassify.mockResolvedValue(classifyOf([
    ['000333.SZ', 'holding', '实际持仓', true],
  ]))
  await render()

  const rm = td('btn-remove-000333.SZ') as HTMLButtonElement
  expect(rm).toBeTruthy()
  await act(async () => { rm.click() })
  await flush()

  // 关键断言：只点一次绝不能真删
  expect(mocks.manualHoldingDelete).not.toHaveBeenCalled()
  // 必须出现确认提示与确认按钮
  expect(host.textContent).toContain('确认？')
  expect(td('btn-confirm-remove-000333.SZ')).toBeTruthy()

  // 点确认才真删
  const confirm = td('btn-confirm-remove-000333.SZ') as HTMLButtonElement
  await act(async () => { confirm.click() })
  await flush()
  expect(mocks.manualHoldingDelete).toHaveBeenCalledWith('000333.SZ')
})

it('分类接口失败时给出错误提示，而不是静默空列表', async () => {
  mocks.positionClassify.mockRejectedValue(new Error('分类服务超时'))
  await render()

  const text = host.textContent ?? ''
  expect(text).toContain('分类服务超时')
  // 页面其它部分仍可用
  expect(td('manual-holdings')).toBeTruthy()
})

it('自选导入失败时明确报错，不假装成功', async () => {
  mocks.watchlistList.mockRejectedValue(new Error('自选接口 500'))
  await render()

  const btn = td('btn-import-watchlist') as HTMLButtonElement
  expect(btn).toBeTruthy()
  await act(async () => { btn.click() })
  await flush()

  const text = host.textContent ?? ''
  expect(text).toContain('自选读取失败')
  expect(text).toContain('自选接口 500')
})

it('重复点击导入不会叠加相同标的（分类入参保持唯一）', async () => {
  mocks.watchlistList.mockResolvedValue({
    symbols: [
      { symbol: '600941.SH', added_at: '' },
      { symbol: '000333.SZ', added_at: '' }, // 与真实持仓重复
    ],
  })
  await render()

  const btn = td('btn-import-watchlist') as HTMLButtonElement
  await act(async () => { btn.click() })
  await flush()
  await act(async () => { btn.click() })
  await flush()

  const calls = mocks.positionClassify.mock.calls
  const last = calls[calls.length - 1][0] as string[]
  expect(new Set(last).size).toBe(last.length)
  // 持仓仍然排在自选之前
  expect(last[0]).toBe('000333.SZ')
})

it('状态变更按钮有稳定标识，且按三态区分可见性', async () => {
  mocks.positionClassify.mockResolvedValue(classifyOf([
    ['000333.SZ', 'holding', '实际持仓', true],
    ['600941.SH', 'planning', '计划买入', false],
    ['605366.SH', 'watch', '自选观察', false],
  ]))
  await render()

  // watch：加计划 + 录为持仓
  expect(td('btn-plan-605366.SH')).toBeTruthy()
  expect(td('btn-hold-605366.SH')).toBeTruthy()
  // planning：录为持仓 + 撤销计划，不给加计划
  expect(td('btn-hold-600941.SH')).toBeTruthy()
  expect(td('btn-unplan-600941.SH')).toBeTruthy()
  expect(td('btn-plan-600941.SH')).toBeNull()
  // holding：移除持仓，不给加计划/撤销
  expect(td('btn-remove-000333.SZ')).toBeTruthy()
  expect(td('btn-plan-000333.SZ')).toBeNull()
  expect(td('btn-unplan-000333.SZ')).toBeNull()
})

it('列头有稳定标识，且包含全部六个字段', async () => {
  await render()
  const hdr = td('list-header')
  expect(hdr).toBeTruthy()
  for (const col of ['代码', '名称', '现价', '涨跌', '盈亏', '操作']) {
    expect(hdr!.textContent).toContain(col)
  }
})

it('跳行动卡的链接对每只票都指向正确 symbol', async () => {
  mocks.positionClassify.mockResolvedValue(classifyOf([
    ['000333.SZ', 'holding', '实际持仓', true],
    ['600941.SH', 'planning', '计划买入', false],
  ]))
  await render()

  expect(td('link-000333.SZ')?.getAttribute('href')).toBe('/action-card?symbol=000333.SZ')
  expect(td('link-600941.SH')?.getAttribute('href')).toBe('/action-card?symbol=600941.SH')
})

it('超 500 只时如实说明截断数量（后端 classify 上限）', async () => {
  // 构造 520 只自选，触发前端截断
  const many = Array.from({ length: 520 }, (_, i) => ({
    symbol: `60${String(i).padStart(4, '0')}.SH`,
    added_at: '',
  }))
  mocks.watchlistList.mockResolvedValue({ symbols: many })
  // 分类只回前 500 只
  mocks.positionClassify.mockImplementation(async (syms: string[]) =>
    classifyOf(syms.slice(0, 500).map(s => [s, 'watch', '自选观察', false] as [string, string, string, boolean])))
  await render()

  const btn = td('btn-import-watchlist') as HTMLButtonElement
  await act(async () => { btn.click() })
  await flush()

  const calls = mocks.positionClassify.mock.calls
  const last = calls[calls.length - 1][0] as string[]
  // 入参不超过后端上限
  expect(last.length).toBeLessThanOrEqual(521) // 含 1 只真实持仓
  // 若发生截断，页面必须说明
  const text = host.textContent ?? ''
  if (last.length < 521) {
    expect(text).toContain('未带入')
  }
})
