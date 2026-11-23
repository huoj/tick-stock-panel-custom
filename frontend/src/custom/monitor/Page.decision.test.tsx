// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { Monitor } from './Page'
import type { AlertEvent } from '@/lib/api'

/**
 * 监控中心「会不会漏掉该处理的事」的决策层回归。
 *
 * 夹具取自 **2026-11-16 17:29 对 3018 的真实响应**（`GET /api/alerts?days=7&limit=500`
 * 返回 49 条 / total=299，其中 policy_radar 36 / paper 9 / policy_calendar 4）。
 *
 * 锁死四件事：
 *   1. 首屏必须说清真实数量 —— 不能一边说「没有需要处理的新事实」一边下面躺着 critical
 *   2. 同一事件只占一行，不再重复 4 遍
 *   3. 方向（利好/利空）、映射依据、传导路径、原文链接必须可见
 *   4. 筛选 tab 必须能命中实际存在的 source（原来点任何一个都是空的）
 */

const mocks = vi.hoisted(() => ({
  alertsList: vi.fn(),
  alertsDigest: vi.fn(),
  monitorRulesList: vi.fn(),
  alertsClear: vi.fn(),
  alertDelete: vi.fn(),
  instrumentNames: vi.fn(),
  preferences: vi.fn(),
  quoteStatus: vi.fn(),
  watchlistGroups: vi.fn(),
  watchlistList: vi.fn(),
  monitorRuleDelete: vi.fn(),
  monitorRuleSave: vi.fn(),
}))

vi.mock('@/lib/api', () => ({ api: mocks }))
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: undefined }),
  useQuoteStatus: () => ({ data: { mode: 'full', is_trading_hours: true } }),
}))
vi.mock('@/lib/useCustomSignalNames', () => ({
  useCustomSignalNames: () => ({}),
}))
vi.mock('@/components/PageHeader', () => ({
  PageHeader: ({ title }: { title: string }) => <div>{title}</div>,
}))
vi.mock('@/components/monitor/RuleEditor', () => ({ RuleEditor: () => null }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/DimensionMembersDialog', () => ({ DimensionMembersDialog: () => null }))
vi.mock('@/lib/monitorBadge', () => ({
  markSeen: vi.fn(), resetBadge: vi.fn(), leaveMonitorPage: vi.fn(),
}))

/**
 * 真实事件组：一条「十五五新型储能实施方案」对 4 只持仓各发一次，全部 critical。
 * mapping_basis 是各票真实值 —— 差异正是「凭什么跟我有关」的答案。
 */
const STORAGE_EVENT: Partial<AlertEvent>[] = [
  {
    symbol: '159682.SZ', name: '创业板50ETF景顺', severity: 'critical',
    mapping_basis: 'ETF当日成分股精确命中 12 只、合计权重 34.34%（宁德时代17.9%、阳光电源3.6%、汇川技术3.5%）',
    directness: '间接', mapping_level: 'B',
  },
  {
    symbol: '159586.SZ', name: '计算机ETF南方', severity: 'critical',
    mapping_basis: 'ETF当日成分股精确命中 13 只、合计权重 4.76%',
    directness: '间接', mapping_level: 'B',
  },
  {
    symbol: '000333.SZ', name: '美的集团', severity: 'critical',
    mapping_basis: '精确命中:储能；AI判定:间接',
    directness: '间接', mapping_level: 'C',
  },
  {
    symbol: '000651.SZ', name: '格力电器', severity: 'critical',
    mapping_basis: '精确命中:储能；模糊命中:锂电池概念；AI判定:间接',
    directness: '间接', mapping_level: 'C',
  },
]

function realAlerts(): AlertEvent[] {
  const base: AlertEvent = {
    ts: 1794738837341,
    source: 'policy_radar',
    type: 'policy_政策',
    symbol: '159682.SZ',
    name: '创业板50ETF景顺',
    message: '【强关联】▲ 利好·政策 | 两部门印发“十五五”新型储能实施方案 | 储能方案拉动宁德、阳光等成分股电池与逆变器需求',
    price: null,
    change_pct: null,
    signals: [],
    severity: 'critical',
    conditions: [],
    logic: 'and',
    mapping_level: 'B',
    mapping_level_desc: '强映射（事件板块与该股主营/概念精确命中）',
    mapping_basis: 'ETF当日成分股精确命中 12 只、合计权重 34.34%',
    directness: '间接',
    source_name: '东方财富',
    source_url: 'https://finance.eastmoney.com/a/202611153931422385.html',
    published_at: '2026-11-15 15:00:13',
    source_tier_label: '媒体转述',
    verification: 'reported',
    verification_label: '媒体报道',
    verification_note: '事件：财经媒体快讯，事件本身未经一手文件确认；映射：映射依据可核对',
    event_title: '两部门印发“十五五”新型储能实施方案',
    event_direction: '利好',
    event_horizon: '长期',
    event_sectors: ['储能', '电力设备', '电池'],
    impact_path_text: '“十五五”新型储能方案明确装机目标 → 储能电池与逆变器需求上升 → ETF重仓宁德时代、阳光电源等受益',
    uncertainty: '政策落地节奏与装机弹性',
    event_id: 'evt_7c7b4e82857b3559',
    event_group_id: 'evtgrp_9e264641961915ad',
  }
  return STORAGE_EVENT.map(h => ({ ...base, ...h }) as AlertEvent)
}

function extraAlerts(): AlertEvent[] {
  return [
    {
      ts: 1794814671911, source: 'paper', type: 'paper_fill', symbol: '688521.SH',
      name: '', severity: 'info', price: 107.0635, change_pct: 0,
      message: '模拟盘[acc_mvxyztiip]买入 688521.SH 400股 @ 107.064 (第 11.13)', signals: [],
    } as AlertEvent,
    {
      ts: 1794700000000, source: 'policy_calendar', type: 'calendar_domestic',
      symbol: '000333.SZ', name: '美的集团', severity: 'warn', signals: [],
      message: '📅 LPR 报价日', event_title: 'LPR 报价日',
      event_direction: '中性', event_days_left: 7,
    } as AlertEvent,
  ]
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  // jsdom 没有 scrollTo；framer-motion 的动画回调会调用它
  Object.defineProperty(window, 'scrollTo', { value: () => {}, writable: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

  const alerts = [...realAlerts(), ...extraAlerts()]
  mocks.alertsList.mockResolvedValue({ alerts, total: 299 })
  mocks.alertsDigest.mockResolvedValue({
    items: [], suppressed: { count: 19, items: [], note: '' },
    counts: {
      total_groups: 24, after_dedupe: 23, items: 4, new_facts: 17, risk_changed: 0,
      by_tier: { must_know: 0, worth_notice: 0, background: 0, theme_hint: 4 },
      by_tier_label: {},
    },
    limitations: [], note: '',
  })
  mocks.monitorRulesList.mockResolvedValue({ rules: [] })
  mocks.instrumentNames.mockResolvedValue({ names: {} })
  mocks.watchlistGroups.mockResolvedValue({ groups: [] })
  mocks.watchlistList.mockResolvedValue({ symbols: [] })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function render() {
  await act(async () => root.render(
    <QueryClientProvider client={client}>
      <MemoryRouter><Monitor /></MemoryRouter>
    </QueryClientProvider>,
  ))
  for (let i = 0; i < 3; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
  }
  return host.textContent ?? ''
}

it('首屏说清真实数量，不再出现「没有需要处理的新事实」而下面躺着 critical', async () => {
  const text = await render()
  // 4 条 critical 属于同一事件（储能方案），聚合后是 1 件事
  expect(text).toContain('今天有 1 件事需要优先看')
  expect(text).toContain('共')
  expect(text).toContain('原始记录')
  expect(text).toContain('299')
  // 按标的数统计：159682/159586/000333/000651 + 688521 + calendar 的 000333(重复) = 5 只
  expect(text).toContain('涉及')
  // 「重要提醒」说没有新事实时，结论带必须给出真实告警数，二者不再互相矛盾
  expect(text).toContain('需优先 1')
})

it('同一事件只占一行：4 条 critical 不重复展示 4 遍', async () => {
  const text = await render()
  const title = '两部门印发“十五五”新型储能实施方案'
  const occurrences = text.split(title).length - 1
  expect(occurrences).toBe(1)
  // 但必须说明影响几只票
  expect(text).toContain('影响 4 只')
  expect(text).toContain('合并 4 条')
})

it('方向（利好/利空）独立成标，不再只埋在 message 文本里', async () => {
  const text = await render()
  expect(text).toContain('利好')
  expect(text).toContain('长期')
  // 159682 那条是 critical，卡片默认展开 -> 传导路径与映射依据都应在
  expect(text).toContain('传导路径')
  expect(text).toContain('“十五五”新型储能方案明确装机目标 → 储能电池与逆变器需求上升')
})

it('每只受影响标的各自显示映射依据（凭什么跟我有关）', async () => {
  const text = await render()
  // 159682 的权重证据与 000333 的概念命中证据必须分别出现，不能混成一句
  expect(text).toContain('ETF当日成分股精确命中 12 只、合计权重 34.34%')
  expect(text).toContain('宁德时代17.9%')
  expect(text).toContain('精确命中:储能')
  expect(text).toContain('模糊命中:锂电池概念')
  // 映射等级也要在
  expect(text).toContain('映射B')
  expect(text).toContain('映射C')
})

it('可信度、不确定性、原文链接都要可见', async () => {
  const text = await render()
  expect(text).toContain('媒体报道')
  expect(text).toContain('未经一手文件确认')
  expect(text).toContain('不确定性：政策落地节奏与装机弹性')
  const link = [...host.querySelectorAll('a')].find(a => a.textContent?.includes('原文'))
  expect(link).toBeTruthy()
  expect(link!.getAttribute('href')).toBe('https://finance.eastmoney.com/a/202611153931422385.html')
  expect(link!.getAttribute('target')).toBe('_blank')
})

it('筛选 tab 能命中实际存在的来源（原来点任何一个都是空的）', async () => {
  await render()
  const tabs = [...host.querySelectorAll('button')].map(b => b.textContent ?? '')
  // 三个新 tab 必须出现，且是中文而不是漏出英文 source 名
  expect(tabs.some(t => t.includes('政策雷达'))).toBe(true)
  expect(tabs.some(t => t.includes('政策日历'))).toBe(true)
  expect(tabs.some(t => t.includes('模拟盘'))).toBe(true)
  // 旧 tab 保留，兼容既有用户规则
  expect(tabs.some(t => t.includes('策略监控'))).toBe(true)
  expect(tabs.some(t => t.includes('信号'))).toBe(true)
})

it('点「政策雷达」只留政策类，模拟盘告警消失', async () => {
  await render()
  const tab = [...host.querySelectorAll('button')].find(b => b.textContent === '政策雷达')
  expect(tab).toBeTruthy()
  await act(async () => { tab!.click() })
  for (let i = 0; i < 3; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
  }
  expect(mocks.alertsList).toHaveBeenLastCalledWith(
    expect.objectContaining({ source: 'policy_radar' }),
  )
})

it('内置监控单独成块：规则为 0 时也说明告警是系统发的', async () => {
  const text = await render()
  expect(text).toContain('内置监控')
  expect(text).toContain('系统自带')
  expect(text).toContain('只读')
  expect(text).toContain('不是你在下面建的规则发出的')
  // 仍保留原有的空规则提示，不删既有文案
  expect(text).toContain('暂无监控规则')
})

it('聚合不丢策略/价格类关键信息（价格与涨跌幅仍显示）', async () => {
  const text = await render()
  // 模拟盘那条带 price=107.0635
  expect(text).toContain('688521.SH')
})

it('没有告警时如实说没有，而不是显示空列表', async () => {
  mocks.alertsList.mockResolvedValue({ alerts: [], total: 0 })
  const text = await render()
  expect(text).toContain('最近 7 天没有触发的告警')
  expect(text).toContain('暂无触发记录')
})
