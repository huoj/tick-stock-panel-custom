// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { AlertRelationBadges } from './AlertRelationBadges'
import type { AlertEvent } from '@/lib/api'

function ev(over: Partial<AlertEvent> = {}): AlertEvent {
  return {
    ts: 1794479704775,
    source: 'policy_radar',
    type: 'policy_产业',
    symbol: '605366.SH',
    name: '宏柏新材',
    message: '【弱关联·推测】▲ 利好·政策 | 上海推动大兆瓦海上风机批量应用',
    ...over,
  } as AlertEvent
}

let host: HTMLDivElement
let root: Root

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

async function render(props: { ev: AlertEvent }) {
  await act(async () => root.render(<AlertRelationBadges {...props} />))
}

function text() {
  return host.textContent ?? ''
}

it('弱关联推断必须显示为映射 C + 间接，不能和公告一样直接', async () => {
  await render({
    ev: ev({
      mapping_level: 'C',
      mapping_level_desc: '弱映射（仅同行业或边缘业务，需多级传导）',
      directness: '间接',
      verification: 'inferred',
      verification_label: '系统推断',
    }),
  })
  const t = text()
  expect(t).toContain('映射 C 弱')
  expect(t).toContain('关系 间接·推断')
  expect(t).toContain('核实 系统推断')
})

it('直接映射用独立样式', async () => {
  await render({
    ev: ev({ mapping_level: 'A', directness: '直接', verification: 'reported', verification_label: '已报道' }),
  })
  const t = text()
  expect(t).toContain('映射 A 直接')
  expect(t).toContain('关系 直接')
  expect(host.querySelector('.text-accent')).not.toBeNull()
})

it('来源与发布日期要显示，链接可点', async () => {
  await render({
    ev: ev({
      mapping_level: 'C',
      directness: '间接',
      source_name: '东方财富',
      source_url: 'https://finance.eastmoney.com/a/1.html',
      published_at: '2026-11-12 15:17:00',
      source_tier_label: '媒体转述',
    }),
  })
  const t = text()
  expect(t).toContain('发布于 2026-11-12 15:17:00')
  expect(t).toContain('东方财富')
  expect(t).toContain('（媒体转述）')
  const link = host.querySelector('a')
  expect(link?.getAttribute('href')).toBe('https://finance.eastmoney.com/a/1.html')
})

it('传导路径折叠展示，展开后有完整链条与不确定处', async () => {
  await render({
    ev: ev({
      mapping_level: 'C',
      directness: '间接',
      impact_path_text:
        '上海推动大兆瓦海上风机批量应用 → 风电叶片用玻纤/复材需求增加 → 宏柏硅烷偶联剂需求受益',
      mapping_basis: '模糊命中:风电；AI判定:间接',
      uncertainty: '意向未转化为具体订单',
    }),
  })
  const t = text()
  expect(t).toContain('为什么相关 / 传导路径')
  expect(t).toContain('传导：上海推动大兆瓦海上风机批量应用')
  expect(t).toContain('判定依据：模糊命中:风电；AI判定:间接')
  expect(t).toContain('不确定处：意向未转化为具体订单')
})

it('缺映射等级时写「未提供」，不默认成直接', async () => {
  await render({ ev: ev({ directness: '间接' }) })
  const t = text()
  expect(t).toContain('映射等级未提供')
  expect(t).not.toContain('映射 A 直接')
})

it('政策类来源完全没有映射信息时明确说明无法判断', async () => {
  await render({ ev: ev({}) })
  expect(text()).toContain(
    '采集侧未提供映射等级与传导路径 —— 无法判断这条跟本标的有多近，请自行核对原文',
  )
})

it('非政策来源没有映射信息时不渲染空壳', async () => {
  await render({ ev: ev({ source: 'paper' }) })
  expect(host.innerHTML).toBe('')
})
