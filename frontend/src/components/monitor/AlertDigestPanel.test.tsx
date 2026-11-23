// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type AlertDigest, type AlertDigestItem } from '@/lib/api'
import { AlertDigestPanel } from './AlertDigestPanel'

vi.mock('@/lib/api', () => ({ api: { alertsDigest: vi.fn() } }))

const TS = Date.parse('2026-11-10T17:01:19+08:00')

function baseItem(over: Partial<AlertDigestItem> = {}): AlertDigestItem {
  return {
    event_group_id: 'eg_1',
    grouped: true,
    event_id: 'ev_1',
    symbols: ['605366.SH'],
    names: ['宏柏新材'],
    last_ts: TS,
    first_ts: TS,
    repeat_count: 1,
    is_new_fact: true,
    risk_changed: false,
    change_note: null,
    hit_rule: null,
    tier: 'theme_hint',
    tier_label: '主题线索',
    tier_reason: '关系未判定，按线索处理',
    tier_description: '只在有明确传导路径时才值得关注',
    what_happened: {
      title: '上海推动大兆瓦海上风机批量应用',
      event_date: '2026-11-10',
      published_at: '2026-11-10 17:01:19',
      sources: ['policy_radar'],
      source_count: 1,
      verification: 'inferred',
      verification_label: '推断',
      alert_count: 1,
      missing: [],
    },
    why_related: {
      symbols: ['605366.SH'],
      relation: 'inferred',
      relation_label: '间接推断',
      relation_reason: '模糊命中:风电；AI判定:间接',
      mapping_levels: ['C'],
      direction: '利好',
    },
    impact_channel: {
      channel_text: '上海推动大兆瓦海上风机批量应用 → 风电叶片用玻纤/复材需求增加 → 宏柏硅烷偶联剂需求受益',
      channel_kind: 'inferred',
      available: true,
      note: '',
    },
    action: {
      needs_action: false,
      label: '仅需关注',
      hint: '没有任何需要你现在处理的动作',
    },
    ...over,
  }
}

function makeDigest(items: AlertDigestItem[], over: Partial<AlertDigest> = {}): AlertDigest {
  const byTier: Record<string, number> = {
    must_know: 0, worth_notice: 0, background: 0, theme_hint: 0,
  }
  for (const it of items) byTier[it.tier] = (byTier[it.tier] ?? 0) + 1
  return {
    items,
    suppressed: { count: 0, items: [], note: '没有需要等你决定的动作' },
    counts: {
      total_groups: items.length + 5,
      after_dedupe: items.length + 2,
      items: items.length,
      new_facts: items.length,
      risk_changed: 0,
      by_tier: byTier as AlertDigest['counts']['by_tier'],
      by_tier_label: {},
    },
    limitations: ['只查最近 7 天（存储侧上限 7 天）；更早的告警不在本次结果里'],
    note: '提醒按事件聚合后去重，只推送新事实或风险变化',
    ...over,
  }
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
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle(ms = 20) {
  await act(async () => { await new Promise(r => setTimeout(r, ms)) })
}

async function render(props: Parameters<typeof AlertDigestPanel>[0] = {}) {
  await act(async () => root.render(
    <QueryClientProvider client={client}><AlertDigestPanel {...props} /></QueryClientProvider>,
  ))
  await settle()
}

async function click(text: string) {
  const el = [...host.querySelectorAll('button')].find(b => b.textContent?.includes(text))
  if (!el) throw new Error(`未找到按钮「${text}」`)
  await act(async () => { el.click() })
  await settle()
}

it('主题线索默认不显示，只给一个可展开的入口', async () => {
  vi.mocked(api.alertsDigest).mockResolvedValue(makeDigest([baseItem()]))
  await render()
  const text = host.textContent ?? ''
  // 标题本身不出现（面板头只显示"线索 1"）
  expect(text).not.toContain('上海推动大兆瓦海上风机批量应用')
  expect(text).toContain('显示 1 条主题线索（弱关联，仅作参考）')
  expect(text).toContain('线索 1')
})

it('展开主题线索后才显示弱关联告警', async () => {
  vi.mocked(api.alertsDigest).mockResolvedValue(makeDigest([baseItem()]))
  await render()
  await click('显示 1 条主题线索')
  expect(host.textContent).toContain('上海推动大兆瓦海上风机批量应用')
})

it('需要知道的告警默认展开，并显示四问', async () => {
  vi.mocked(api.alertsDigest).mockResolvedValue(makeDigest([
    baseItem({
      tier: 'must_know',
      tier_label: '需要知道',
      tier_reason: '公司自身事件且已核实',
      what_happened: {
        ...baseItem().what_happened,
        title: '宏柏新材:股东减持股份计划公告',
        verification: 'fact',
        verification_label: '事实',
      },
      why_related: {
        ...baseItem().why_related,
        relation: 'direct',
        relation_label: '直接相关',
        relation_reason: '公告主体就是这家公司',
      },
      action: { needs_action: true, label: '需要处理', hint: '核对减持规模与时间窗口' },
    }),
  ]))
  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('宏柏新材:股东减持股份计划公告')
  expect(text).toContain('发生了什么')
  expect(text).toContain('为什么相关')
  expect(text).toContain('影响渠道')
  expect(text).toContain('直接相关')
  expect(text).toContain('需要处理')
  expect(text).toContain('核对减持规模与时间窗口')
  expect(text).toContain('为何是这个档：公司自身事件且已核实')
})

it('来源与日期缺失时如实写未提供，不留白', async () => {
  vi.mocked(api.alertsDigest).mockResolvedValue(makeDigest([
    baseItem({
      tier: 'must_know',
      what_happened: {
        ...baseItem().what_happened,
        sources: [],
        source_count: 0,
        verification: null,
        verification_label: null,
      },
      impact_channel: {
        channel_text: '未提供',
        channel_kind: null,
        available: false,
        note: '采集侧没有给出传导路径',
      },
      why_related: {
        ...baseItem().why_related,
        relation: 'unknown',
        relation_label: '未判定',
        relation_reason: '采集侧未给出映射关系',
        mapping_levels: [],
        direction: 'unknown',
      },
    }),
  ]))
  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('来源 未提供')
  expect(text).toContain('（未核实）')
  expect(text).toContain('未判定')
  expect(text).toContain('采集侧没有给出传导路径')
})

it('重复采集与风险变化都有可见标记', async () => {
  vi.mocked(api.alertsDigest).mockResolvedValue(makeDigest([
    baseItem({
      tier: 'worth_notice',
      tier_label: '值得留意',
      repeat_count: 3,
      risk_changed: true,
      change_note: '核实状态升级：inferred → reported',
    }),
  ]))
  await render()
  // 非 must_know 行默认折叠, 先点标题展开看详情
  await click('上海推动大兆瓦海上风机批量应用')
  const text = host.textContent ?? ''
  expect(text).toContain('重复 3 次')
  expect(text).toContain('变化')
  expect(text).toContain('核实状态升级：inferred → reported')
})

it('没有需要处理的新事实时明确说明，而不是显示空列表', async () => {
  vi.mocked(api.alertsDigest).mockResolvedValue(makeDigest([]))
  await render()
  expect(host.textContent).toContain('最近 7 天没有需要你处理的新事实')
})

it('档位计数与去重统计都要显示', async () => {
  vi.mocked(api.alertsDigest).mockResolvedValue(makeDigest([
    baseItem({ event_group_id: 'a', tier: 'must_know', tier_label: '需要知道' }),
    baseItem({ event_group_id: 'b', tier: 'worth_notice', tier_label: '值得留意' }),
    baseItem({ event_group_id: 'c', tier: 'theme_hint', tier_label: '主题线索' }),
  ]))
  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('需要知道 1')
  expect(text).toContain('值得留意 1')
  expect(text).toContain('线索 1')
  expect(text).toContain('去重后 3/5 条')
})

it('口径限制折叠可展开', async () => {
  vi.mocked(api.alertsDigest).mockResolvedValue(makeDigest([]))
  await render()
  expect(host.textContent).toContain('口径与限制（1）')
  expect(host.textContent).toContain('更早的告警不在本次结果里')
})

it('加载失败时有明确错误提示', async () => {
  vi.mocked(api.alertsDigest).mockRejectedValue(new Error('boom'))
  await render()
  expect(host.textContent).toContain('提醒摘要加载失败')
})

it('带 symbol 时把过滤条件传给后端', async () => {
  vi.mocked(api.alertsDigest).mockResolvedValue(makeDigest([]))
  await render({ symbol: '605366.SH', maxItems: 5 })
  expect(vi.mocked(api.alertsDigest)).toHaveBeenCalledWith(
    expect.objectContaining({ symbol: '605366.SH', maxItems: 5 }),
  )
})

it('面板不出现任何买卖建议字眼', async () => {
  vi.mocked(api.alertsDigest).mockResolvedValue(makeDigest([
    baseItem({ tier: 'must_know', tier_label: '需要知道' }),
  ]))
  await render()
  const text = host.textContent ?? ''
  for (const banned of ['买入建议', '卖出建议', 'buy_score', 'sell_score', 'recommendation', '评级']) {
    expect(text).not.toContain(banned)
  }
})

it('轮询本身不写「已看过」状态', async () => {
  vi.mocked(api.alertsDigest).mockResolvedValue(makeDigest([baseItem({ tier: 'must_know' })]))
  await render()
  // 首次查询只带 ack 缺省，不能出现 ack:true
  for (const call of vi.mocked(api.alertsDigest).mock.calls) {
    expect(call[0]?.ack).not.toBe(true)
  }
  expect(host.textContent).toContain('这些我看过了')
})

it('点「这些我看过了」才 ack，且会重新拉一次', async () => {
  vi.mocked(api.alertsDigest).mockResolvedValue(makeDigest([baseItem({ tier: 'must_know' })]))
  await render()
  const before = vi.mocked(api.alertsDigest).mock.calls.length
  await click('这些我看过了')
  const calls = vi.mocked(api.alertsDigest).mock.calls
  expect(calls.length).toBeGreaterThan(before)
  expect(calls.some(c => c[0]?.ack === true)).toBe(true)
})

it('ack 失败时不假装成功，给出可重试的提示', async () => {
  vi.mocked(api.alertsDigest)
    .mockResolvedValueOnce(makeDigest([baseItem({ tier: 'must_know' })]))
    .mockRejectedValueOnce(new Error('boom'))
  await render()
  await click('这些我看过了')
  expect(host.textContent).toContain('记录失败，可重试')
})
