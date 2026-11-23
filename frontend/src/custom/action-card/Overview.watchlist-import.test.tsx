// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { PositionOverview } from './Overview'

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

function classifyResult(symbols: string[]) {
  return {
    items: symbols.map(symbol => ({
      symbol,
      state: 'watch',
      state_label: '自选观察',
      state_note: '自选观察，不计入盈亏',
      counts_toward_pnl: false,
    })),
    counts: {},
    labels: {},
    note: '',
  }
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

  mocks.manualHoldings.mockResolvedValue({
    holdings: [],
    count: 1,
    cash: null,
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
  mocks.positionClassify.mockImplementation(async (symbols: string[]) => classifyResult(symbols))
  mocks.planCapitalCheck.mockResolvedValue(emptyCapital)
  mocks.watchlistList.mockResolvedValue({ symbols: [
    { symbol: '600941.SH', added_at: '' },
    { symbol: '605366.SH', added_at: '' },
    { symbol: '000333.SZ', added_at: '' }, // 与真实持仓重复 -> 应去重
  ] })
  mocks.validateStrength.mockResolvedValue({})
  mocks.planReconcile.mockResolvedValue({ matches: [] })
  // 三态清单会补名称与最近收盘: 不补这两个 mock 会因「不是函数」直接炸在渲染期。
  mocks.instrumentNames.mockResolvedValue({ names: {
    '000333.SZ': '美的集团',
    '600941.SH': '中国移动',
    '605366.SH': '宏柏新材',
  } })
  mocks.klineDailyBatch.mockResolvedValue({ data: {
    // 两根收盘 -> 现价 90.55, 涨跌幅 = 90.55/88.00 - 1
    '000333.SZ': [
      { symbol: '000333.SZ', date: '2026-11-13', open: 88, high: 89, low: 87, close: 88 },
      { symbol: '000333.SZ', date: '2026-11-14', open: 89, high: 91, low: 88, close: 90.55 },
    ],
    // 只有一根 -> 有现价但没有可比前收, 涨跌幅必须渲染「—」而不是 0
    '600941.SH': [
      { symbol: '600941.SH', date: '2026-11-14', open: 100, high: 101, low: 99, close: 100.5 },
    ],
    // 空数组 -> 现价与涨跌幅都「—」
    '605366.SH': [],
  } })
  // 行内状态写回的写接口: 弹窗打开时不应触发, 只有提交保存才调用。
  mocks.positionPlanSave.mockResolvedValue({})
  mocks.positionPlanDelete.mockResolvedValue({ deleted: '600941.SH' })
  mocks.manualHoldingSave.mockResolvedValue({ ok: true, holding: {} })
  mocks.manualHoldingDelete.mockResolvedValue({ ok: true, symbol: '000333.SZ' })
})

afterEach(async () => {
  await act(async () => root.unmount())
  qc.clear()
  host.remove()
  vi.clearAllMocks()
})

async function flush() {
  // 单次 setTimeout 只够跑完一轮 effect: 分类返回后才发名称/行情两个请求,
  // 它们的返回又要再跑一轮渲染 —— 多轮推进才不会在中间态断言。
  for (let i = 0; i < 6; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 60)) })
  }
}

function button(text: string) {
  return [...host.querySelectorAll('button')].find(b => b.textContent?.includes(text))!
}

async function click(text: string) {
  const b = button(text)
  expect(b).toBeTruthy()
  await act(async () => { b.click(); await new Promise(r => setTimeout(r, 10)) })
}

/** React 18 受控输入: 必须走原生 value setter, 直接改 .value 不会触发 onChange。 */
async function fill(el: Element, value: string) {
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, 'value')!.set!.call(el, value)
    el.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

/** 让 classify 返回三种状态, 用来验证按状态给动作(尤其持仓优先于计划)。 */
function mixedStates() {
  const rows = [
    ['000333.SZ', 'holding', '实际持仓', true],
    ['600941.SH', 'planning', '计划买入', false],
    ['605366.SH', 'watch', '自选观察', false],
  ] as const
  mocks.positionClassify.mockImplementation(async () => ({
    items: rows.map(([symbol, state, state_label, counts_toward_pnl]) => ({
      symbol, state, state_label, state_note: state_label, counts_toward_pnl,
    })),
    counts: {}, labels: {}, note: '',
  }))
}

it('导入自选把清单并进三态查看，保留已录入真实持仓并去重', async () => {
  await act(async () => root.render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><PositionOverview /></MemoryRouter>
    </QueryClientProvider>,
  ))
  await flush()

  // 初始：自动带入上方「我的真实持仓」里的 000333.SZ
  expect(mocks.positionClassify).toHaveBeenCalled()
  const firstCall = mocks.positionClassify.mock.calls.find(
    (c: unknown[]) => Array.isArray(c[0]) && (c[0] as string[]).includes('000333.SZ'),
  )
  expect(firstCall).toBeTruthy()

  // 点击「导入自选」
  await act(async () => button('导入自选').click())
  await flush()

  const calls = mocks.positionClassify.mock.calls
  const last = calls[calls.length - 1][0] as string[]
  // 真实持仓 + 自选合并去重（000333.SZ 只出现一次）, 且持仓排在前面
  expect(last).toEqual(['000333.SZ', '600941.SH', '605366.SH'])
  expect(new Set(last).size).toBe(last.length)
  // 诚实边界：导入不等于持仓
  expect(host.textContent).toContain('自选仅为')
  // 三态里确实渲染了新带入的自选代码
  expect(host.textContent).toContain('600941.SH')
})

it('三态清单补名称/现价/涨跌幅, 并给出跳行动卡的链接', async () => {
  await act(async () => root.render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><PositionOverview /></MemoryRouter>
    </QueryClientProvider>,
  ))
  await flush()

  // 名称与行情都按 effectiveSymbols 一次性批量拉, 不是每行一个请求
  expect(mocks.instrumentNames).toHaveBeenCalled()
  expect(mocks.klineDailyBatch).toHaveBeenCalled()
  expect(mocks.klineDailyBatch.mock.calls[0][0]).toEqual(['000333.SZ'])

  const row = [...host.querySelectorAll<HTMLElement>('div')].find(
    d => d.className === 'flex items-center gap-2 px-3 py-1.5'
      && d.textContent?.startsWith('000333.SZ'),
  )!
  expect(row).toBeTruthy()
  expect(row.textContent).toContain('美的集团')
  // 两根收盘 -> 现价 90.55 / 涨跌幅 +2.90%
  expect(row.textContent).toContain('90.55')
  expect(row.textContent).toContain('+2.90%')

  const link = row.querySelector('a')!
  expect(link.getAttribute('href')).toBe('/action-card?symbol=000333.SZ')
})

/** 找到某只票的整行(信息行 + 状态变更行)的外层容器。 */
function itemRow(symbol: string): HTMLElement {
  const info = [...host.querySelectorAll<HTMLElement>('div')].find(
    d => d.className === 'flex items-center gap-2 px-3 py-1.5'
      && d.textContent?.startsWith(symbol),
  )!
  expect(info).toBeTruthy()
  return info.parentElement as HTMLElement
}

function renderOverview() {
  return act(async () => root.render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><PositionOverview /></MemoryRouter>
    </QueryClientProvider>,
  ))
}

it('按三态给状态变更动作: watch 可加计划/录持仓, planning 可撤销, holding 不给加计划', async () => {
  mixedStates()
  await renderOverview()
  await flush()

  const watch = itemRow('605366.SH').textContent ?? ''
  expect(watch).toContain('+ 加计划')
  expect(watch).toContain('录为持仓')

  const planning = itemRow('600941.SH').textContent ?? ''
  expect(planning).toContain('录为持仓')
  expect(planning).toContain('撤销计划')
  expect(planning).not.toContain('+ 加计划')

  // 后端 resolve_state 里持仓优先于计划, 给真仓「加计划」无意义。
  const holding = itemRow('000333.SZ').textContent ?? ''
  expect(holding).toContain('移除持仓')
  expect(holding).not.toContain('+ 加计划')
  expect(holding).not.toContain('撤销计划')
})

it('录为持仓走 manualHoldingSave, 空值不提交, 成功后失效分类/持仓/组合风险', async () => {
  mixedStates()
  await renderOverview()
  await flush()

  // watch 行点「录为持仓」; 用该行自己的按钮, 不误点 planning 行的同名按钮。
  const watchRow = itemRow('605366.SH')
  const holdBtn = [...watchRow.querySelectorAll('button')].find(b => b.textContent?.includes('录为持仓'))!
  await act(async () => { holdBtn.click(); await new Promise(r => setTimeout(r, 10)) })

  expect(host.textContent).toContain('录入真实持仓')
  // 数量/成本为空时禁用, 不产生写请求。
  expect(button('保存持仓').hasAttribute('disabled')).toBe(true)
  expect(mocks.manualHoldingSave).not.toHaveBeenCalled()

  await fill(host.querySelector('[aria-label="持仓数量"]')!, '500')
  await fill(host.querySelector('[aria-label="平均成本"]')!, '12.5')
  await click('保存持仓')
  await flush()

  expect(mocks.manualHoldingSave).toHaveBeenCalledWith('605366.SH', {
    qty: 500, avg_cost: 12.5, entry_date: null,
  })
  // 写回后必须重拉分类(状态从 watch 变成 holding)与真仓汇总。
  expect(mocks.manualHoldings.mock.calls.length).toBeGreaterThan(1)
  expect(mocks.positionClassify.mock.calls.length).toBeGreaterThan(1)
})

it('加计划把空数字字段转成 null, 不传 0/NaN (后端 validate_plan 是 fail-closed)', async () => {
  mixedStates()
  await renderOverview()
  await flush()

  const watchRow = itemRow('605366.SH')
  const planBtn = [...watchRow.querySelectorAll('button')].find(b => b.textContent?.includes('+ 加计划'))!
  await act(async () => { planBtn.click(); await new Promise(r => setTimeout(r, 10)) })

  expect(host.textContent).toContain('登记买入计划')
  // 全空时禁用: 计划至少要有一条备注或一个字段。
  expect(button('保存计划').hasAttribute('disabled')).toBe(true)

  await fill(host.querySelector('[aria-label="计划备注"]')!, '等回踩再买')
  await click('保存计划')
  await flush()

  expect(mocks.positionPlanSave).toHaveBeenCalledWith('605366.SH', {
    symbol: '605366.SH',
    planned_hold_days: null,
    acceptable_loss_pct: null,
    batches: [],
    note: '等回踩再买',
  })
})

it('撤销计划走 positionPlanDelete; 移除持仓需要二次确认', async () => {
  mixedStates()
  await renderOverview()
  await flush()

  const planRow = itemRow('600941.SH')
  const delPlanBtn = [...planRow.querySelectorAll('button')].find(b => b.textContent?.includes('撤销计划'))!
  await act(async () => { delPlanBtn.click(); await new Promise(r => setTimeout(r, 10)) })
  await flush()
  expect(mocks.positionPlanDelete).toHaveBeenCalledWith('600941.SH')

  // 移除持仓: 第一次点只出确认条, 确认后才真删。
  const holdRow = itemRow('000333.SZ')
  const rmBtn = [...holdRow.querySelectorAll('button')].find(b => b.textContent?.includes('移除持仓'))!
  await act(async () => { rmBtn.click(); await new Promise(r => setTimeout(r, 10)) })
  expect(host.textContent).toContain('确认？')
  expect(mocks.manualHoldingDelete).not.toHaveBeenCalled()
  await click('确认移除')
  await flush()
  expect(mocks.manualHoldingDelete).toHaveBeenCalledWith('000333.SZ')
})

it('从自选录持仓时平均成本可填负数(做 T 摊薄), 0 与无效值仍拒绝', async () => {
  mixedStates()
  await renderOverview()
  await flush()

  // 用户路径: 自选观察行 -> 「录为持仓」弹窗。
  const watchRow = itemRow('605366.SH')
  const holdBtn = [...watchRow.querySelectorAll('button')].find(b => b.textContent?.includes('录为持仓'))!
  await act(async () => { holdBtn.click(); await new Promise(r => setTimeout(r, 10)) })
  expect(host.textContent).toContain('成本可填负数')

  await fill(host.querySelector('[aria-label="持仓数量"]')!, '500')

  // 成本 0 在录入约定里代表"没填", 仍拒绝。
  await fill(host.querySelector('[aria-label="平均成本"]')!, '0')
  expect(button('保存持仓').hasAttribute('disabled')).toBe(true)

  // 只敲了个 "-" -> NaN, 不能提交(否则 JSON.stringify 会静默变 null 丢掉输入)。
  await fill(host.querySelector('[aria-label="平均成本"]')!, '-')
  expect(button('保存持仓').hasAttribute('disabled')).toBe(true)
  expect(mocks.manualHoldingSave).not.toHaveBeenCalled()

  // 负成本照实录进去, 参数不被钳成 0 / null。
  await fill(host.querySelector('[aria-label="平均成本"]')!, '-5.2')
  expect(button('保存持仓').hasAttribute('disabled')).toBe(false)
  await click('保存持仓')
  await flush()
  expect(mocks.manualHoldingSave).toHaveBeenCalledWith('605366.SH', {
    qty: 500, avg_cost: -5.2, entry_date: null,
  })
})

it('页面有放大字号作用域、白话引导、列头与可折叠的次要信息', async () => {
  await renderOverview()
  await flush()

  // 字号作用域类挂在根上：10/11/xs 小字被 .tsp-readable 统一放大，
  // .tsp-overview 再把明细密集区降回可读的紧凑尺寸，不波及组合风控页。
  expect(host.querySelector('.tsp-readable.tsp-overview')).toBeTruthy()

  // 给普通投资者的大白话引导（PlainGuide 的 intro + 前 3 个摊开的名词）
  expect(host.textContent).toContain('这一页是帮你把')
  expect(host.textContent).toContain('其余')
  expect(host.textContent).toContain('名词的白话解释')

  // 三态清单新增列头，数字不再没有表头
  const header = [...host.querySelectorAll<HTMLElement>('div')].find(
    d => d.className.includes('tsp-compact') && d.textContent?.includes('代码')
      && d.textContent?.includes('名称') && d.textContent?.includes('现价'),
  )
  expect(header).toBeTruthy()

  // 逐票操作区收进折叠：按钮仍在 DOM 里（测试/功能不受影响），但默认不铺满视线
  const detail = [...host.querySelectorAll('details')].find(
    d => d.textContent?.includes('改状态 / 管理这只票'),
  )
  expect(detail).toBeTruthy()
  expect(detail!.querySelector('summary')?.textContent).toContain('改状态')

  // 单票规则检验、计划资金逐票明细都改成了默认收起的进阶/次要信息
  expect([...host.querySelectorAll('details')].some(
    d => d.textContent?.includes('单票规则检验'),
  )).toBe(true)
})
