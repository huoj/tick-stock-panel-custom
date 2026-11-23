// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type DatasetInfo, type DatasetQualityReport } from '@/lib/api'
import { DatasetTrustPanel } from './DatasetTrustPanel'

vi.mock('@/lib/api', () => ({ api: {
  datasetCatalog: vi.fn(), datasetQuality: vi.fn(),
} }))

const kline: DatasetInfo = {
  id: 'kline_daily', label: '股票日K（原始价）', domain: 'kline', layer: 'raw',
  asset_type: 'stock', relpath: 'kline_daily', layout: 'date_partition', cadence: 'daily',
  date_column: 'date', date_format: 'YYYY-MM-DD', currency: 'CNY', timezone: 'Asia/Shanghai',
  source: '数据源插件（按能力矩阵路由）', source_basis: 'routing',
  pit: { mode: 'event_date', known_at_column: 'date', period_column: null, usable: true },
  fields: [
    { name: 'close', unit: '元/股', note: '前复权' },
    { name: 'amount', unit: '元' },
  ],
  notes: ['落盘不含 source 列 —— 无法逐行溯源'],
  key_columns: ['symbol', 'date'],
  freshness: { dataset_id: 'kline_daily', status: 'ok', latest_date: '2026-11-05',
    updated_at: '2026-11-05 15:00:00', rows: 5570, settlement: 'intraday',
    anchor_date: '2026-11-05', lag_sessions: 0,
    note: '最新分区为盘中快照，盘后会被重写' },
}

const etf: DatasetInfo = {
  ...kline, id: 'kline_etf_daily', label: 'ETF日K（原始价）', asset_type: 'etf',
  relpath: 'kline_etf_daily',
  freshness: { dataset_id: 'kline_etf_daily', status: 'stale', latest_date: '2026-11-04',
    updated_at: '2026-11-05 08:15:07', rows: 368965, settlement: 'final',
    anchor_date: '2026-11-05', lag_sessions: 1,
    note: '全市场最新会话 2026-11-05 尚未定版，滞后基准取 2026-11-05' },
}

const forecast: DatasetInfo = {
  ...kline, id: 'ext_forecast', label: '业绩预告', domain: 'ext', relpath: 'ext_data/ext_forecast',
  layout: 'single_file', pit: { mode: 'period_only', known_at_column: null, period_column: 'period', usable: false },
  fields: [{ name: 'forecast_value', unit: '元' }], notes: [], key_columns: ['symbol'],
  freshness: { dataset_id: 'ext_forecast', status: 'ok', latest_date: null,
    updated_at: '2026-11-04 22:00:00', rows: 2019, settlement: 'n/a',
    anchor_date: null, lag_sessions: 0, note: '依据落盘时间判定（本表无日期列）' },
}

const report: DatasetQualityReport = {
  dataset_id: 'kline_daily', checked_at: '2026-11-06T11:00:00+08:00',
  checked_days: ['2026-11-03', '2026-11-04', '2026-11-05'], ok: false,
  finding_counts: { conflict: 141 }, findings: [{
    kind: 'conflict', severity: 'low', scope: '近 3 个已定版分区文件', day: '2026-11-05',
    affected: 141, symbols: ['000159.SZ'], detail: '与 ext_valuation.price 不一致',
  }], limitations: ['异常跳变检查使用不复权列 raw_close（close 为前复权价）'],
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
  vi.mocked(api.datasetCatalog).mockResolvedValue({
    count: 3, session_anchor: '2026-11-05', items: [kline, etf, forecast],
  })
  vi.mocked(api.datasetQuality).mockResolvedValue(report)
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle(ms = 20) {
  await act(async () => { await new Promise(r => setTimeout(r, ms)) })
}

async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <DatasetTrustPanel />
  </QueryClientProvider>))
  // react-query 的 promise 要等一轮宏任务才落地；不等就还是 loading 态。
  await settle()
}

async function click(text: string, nth = 0) {
  const candidates = [...host.querySelectorAll('button')]
    .filter(b => b.textContent?.includes(text))
  const button = candidates[nth]
  if (!button) throw new Error(`未找到按钮「${text}」#${nth}（候选 ${candidates.length} 个）`)
  await act(async () => { button.click(); await new Promise(r => setTimeout(r, 10)) })
  // 点击可能触发新的查询（质量检查），再等一轮让它落地。
  await settle()
}

it('展示每个数据集的来源、最新数据日、更新时间与币种', async () => {
  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('共 3 个数据集')
  expect(text).toContain('数据源插件（按能力矩阵路由）')
  expect(text).toContain('2026-11-05 15:00:00')
  expect(text).toContain('CNY')
  expect(vi.mocked(api.datasetCatalog)).toHaveBeenCalledWith(true)
})

it('把未定版快照、滞后与不可按当时已知取数标出来', async () => {
  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('盘中快照')      // kline_daily settlement=intraday
  expect(text).toContain('滞后')          // kline_etf_daily
  expect(text).toContain('落后 1 个交易日')
  expect(text).toContain('1 个新鲜度异常')
  expect(text).toContain('1 个不可按「当时已知」取数')
  expect(text).toContain('仅报告期')      // ext_forecast
})

it('展开行后展示逐列单位与备注', async () => {
  await render()
  expect(host.textContent).not.toContain('元/股')
  await click('股票日K')
  const text = host.textContent ?? ''
  expect(text).toContain('close')
  expect(text).toContain('元/股')
  expect(text).toContain('amount')
  expect(text).toContain('落盘不含 source 列')
  expect(text).toContain('symbol, date')
})

it('展开行后按需运行质量检查并展示结论', async () => {
  await render()
  expect(vi.mocked(api.datasetQuality)).not.toHaveBeenCalled()
  await click('股票日K')
  await click('运行质量检查')
  expect(vi.mocked(api.datasetQuality)).toHaveBeenCalledWith('kline_daily', 10)
  const text = host.textContent ?? ''
  expect(text).toContain('跨源冲突 141')
  expect(text).toContain('与 ext_valuation.price 不一致')
  expect(text).toContain('异常跳变检查使用不复权列 raw_close')
})

it('「仅异常」筛选只留下新鲜度有问题的数据集', async () => {
  await render()
  await click('仅异常')
  const text = host.textContent ?? ''
  expect(text).toContain('ETF日K（原始价）')
  expect(text).not.toContain('股票日K')
  expect(text).not.toContain('业绩预告')
})

it('「仅不可按当时已知取数」筛选只留下 PIT 不可用的数据集', async () => {
  await render()
  await click('仅不可按')
  const text = host.textContent ?? ''
  expect(text).toContain('业绩预告')
  expect(text).not.toContain('股票日K')
  expect(text).not.toContain('ETF日K')
})

it('目录读取失败时给出错误提示', async () => {
  vi.mocked(api.datasetCatalog).mockRejectedValue(new Error('后端未启动'))
  await render()
  const alert = host.querySelector('[role="alert"]')
  expect(alert?.textContent).toContain('后端未启动')
})
