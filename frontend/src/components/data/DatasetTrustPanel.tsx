import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Loader2, Ruler } from 'lucide-react'
import { api, type DatasetInfo, type DatasetQualityReport } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

type Filter = 'all' | 'problem' | 'no-pit'

const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: '全部' },
  { id: 'problem', label: '仅异常' },
  { id: 'no-pit', label: '仅不可按「当时已知」取数' },
]

const STATUS_TEXT: Record<string, string> = {
  ok: '正常',
  stale: '滞后',
  empty: '无数据',
  absent: '目录缺失',
}

const SETTLEMENT_TEXT: Record<string, string> = {
  final: '已定版',
  intraday: '盘中快照',
  unknown: '未判定',
  'n/a': '不适用',
}

const PIT_TEXT: Record<string, string> = {
  event_date: '日期即可知日',
  announcement: '按公告日',
  period_only: '仅报告期',
  unknown: '无可知日',
}

const SEVERITY_CLASS: Record<string, string> = {
  high: 'text-danger',
  medium: 'text-warning',
  low: 'text-secondary',
}

const KIND_TEXT: Record<string, string> = {
  missing: '缺失',
  duplicate: '重复',
  halt: '停牌',
  adjust: '复权',
  jump: '异常跳变',
  conflict: '跨源冲突',
}

/** 新鲜度是否算问题。空表本身也是问题（用户看不到数据）。 */
function isProblem(row: DatasetInfo): boolean {
  const status = row.freshness?.status
  return status !== undefined && status !== 'ok'
}

function freshnessLabel(row: DatasetInfo): { text: string; className: string } {
  const fresh = row.freshness
  if (!fresh) return { text: '未采集', className: 'text-secondary' }
  const text = STATUS_TEXT[fresh.status] ?? fresh.status
  if (fresh.status === 'ok') return { text, className: 'text-bear' }
  if (fresh.status === 'stale') return { text, className: 'text-warning' }
  return { text, className: 'text-danger' }
}

function CoverageDetail({ report }: { report: DatasetQualityReport }) {
  const kinds = Object.keys(report.finding_counts).filter(k => report.finding_counts[k] > 0)
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-secondary">
        {report.ok ? (
          <span className="inline-flex items-center gap-1 text-bear">
            <CheckCircle2 className="h-3.5 w-3.5" />近 {report.checked_days.length} 个交易日未发现问题
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-warning">
            <AlertTriangle className="h-3.5 w-3.5" />
            {kinds.map(k => `${KIND_TEXT[k] ?? k} ${report.finding_counts[k]}`).join('、')}
          </span>
        )}
      </div>
      {report.findings.length > 0 && (
        <ul className="space-y-1.5">
          {report.findings.map((f, i) => (
            <li key={`${f.kind}-${f.day ?? ''}-${i}`} className="flex flex-wrap items-baseline gap-2 text-xs">
              <span className={SEVERITY_CLASS[f.severity] ?? 'text-secondary'}>
                {KIND_TEXT[f.kind] ?? f.kind}
              </span>
              <span className="text-secondary">{f.day ?? f.scope}</span>
              <span className="text-foreground">{f.affected}</span>
              <span className="text-secondary">{f.detail}</span>
            </li>
          ))}
        </ul>
      )}
      {report.limitations.length > 0 && (
        <ul className="space-y-1 text-[11px] text-secondary">
          {report.limitations.map((l, i) => <li key={i}>· {l}</li>)}
        </ul>
      )}
    </div>
  )
}

/**
 * 数据集可信度面板 —— 每个数据集展示来源、最新数据日与更新时间、币种、
 * 逐列单位、「当时已知」取数口径，并可展开看数据质量检查结果。
 */
export function DatasetTrustPanel() {
  const [filter, setFilter] = useState<Filter>('all')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [qualityFor, setQualityFor] = useState<string | null>(null)

  const catalog = useQuery({
    queryKey: QK.datasetCatalog,
    queryFn: () => api.datasetCatalog(true),
  })
  const quality = useQuery({
    queryKey: [...QK.datasetCatalog, 'quality', qualityFor],
    queryFn: () => api.datasetQuality(qualityFor!, 10),
    enabled: !!qualityFor,
  })

  const items = catalog.data?.items ?? []
  const rows = useMemo(() => {
    if (filter === 'problem') return items.filter(isProblem)
    if (filter === 'no-pit') return items.filter(r => !r.pit.usable)
    return items
  }, [items, filter])

  const problemCount = items.filter(isProblem).length
  const noPitCount = items.filter(r => !r.pit.usable).length

  if (catalog.isPending) {
    return (
      <p className="flex items-center gap-2 text-xs text-secondary">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />正在读取数据集目录…
      </p>
    )
  }
  if (catalog.error) {
    return <p role="alert" className="text-xs text-danger">{(catalog.error as Error).message}</p>
  }

  return (
    <section className="space-y-4" aria-label="数据集可信度">
      <p className="text-xs text-secondary leading-relaxed">
        每个数据集的口径声明：来源、最新数据日与落盘时间、币种、逐列单位，以及能否按「当时已知」
        取数（财报/公告类按公告日 T+1 生效，只有报告期的不参与历史回测）。
        {catalog.data?.session_anchor && ` 全市场最新交易日 ${catalog.data.session_anchor}。`}
      </p>

      <div className="flex flex-wrap items-center gap-3 text-xs">
        <span className="text-secondary">共 {items.length} 个数据集</span>
        {problemCount > 0 && <span className="text-warning">{problemCount} 个新鲜度异常</span>}
        {noPitCount > 0 && <span className="text-secondary">{noPitCount} 个不可按「当时已知」取数</span>}
        <span className="ml-auto flex flex-wrap items-center gap-1">
          {FILTERS.map(f => (
            <button key={f.id} onClick={() => setFilter(f.id)}
              className={`rounded-btn border px-2 py-1 transition-colors ${
                filter === f.id
                  ? 'border-accent/50 bg-accent/10 text-accent'
                  : 'border-border text-secondary hover:bg-elevated hover:text-foreground'
              }`}>
              {f.label}
            </button>
          ))}
        </span>
      </div>

      {rows.length === 0 ? (
        <p className="text-xs text-secondary">
          {filter === 'all' ? '没有数据集。' : '当前筛选下没有数据集。'}
        </p>
      ) : (
        <div className="overflow-x-auto rounded-btn border border-border">
          <table className="w-full min-w-[52rem] text-left text-xs">
            <thead className="border-b border-border bg-elevated/50 text-secondary">
              <tr>
                <th className="px-3 py-2 font-normal">数据集</th>
                <th className="px-3 py-2 font-normal">来源</th>
                <th className="px-3 py-2 font-normal">最新数据</th>
                <th className="px-3 py-2 font-normal">更新时间</th>
                <th className="px-3 py-2 font-normal">币种</th>
                <th className="px-3 py-2 font-normal">当时已知</th>
                <th className="px-3 py-2 font-normal">状态</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(row => {
                const fresh = freshnessLabel(row)
                const open = expanded === row.id
                return (
                  <tr key={row.id} className="border-b border-border/60 align-top last:border-0">
                    <td className="px-3 py-2">
                      <button className="flex items-start gap-1 text-left"
                        onClick={() => setExpanded(open ? null : row.id)}>
                        {open ? <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                              : <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
                        <span>
                          <span className="text-foreground">{row.label}</span>
                          <span className="block font-mono text-[11px] text-secondary">{row.id}</span>
                        </span>
                      </button>
                      {open && (
                        <div className="mt-2 space-y-3 border-l border-border pl-3">
                          <div className="grid gap-1 text-[11px] text-secondary sm:grid-cols-2">
                            <span>路径 <span className="font-mono text-foreground">{row.relpath}</span></span>
                            <span>主键 <span className="font-mono text-foreground">
                              {row.key_columns.length ? row.key_columns.join(', ') : '未声明'}</span></span>
                            <span>时区 <span className="text-foreground">{row.timezone}</span></span>
                            <span>频率 <span className="text-foreground">{row.cadence}</span></span>
                          </div>
                          <div>
                            <div className="mb-1 flex items-center gap-1 text-[11px] text-secondary">
                              <Ruler className="h-3 w-3" />字段单位
                            </div>
                            <ul className="flex flex-wrap gap-1.5">
                              {row.fields.map(f => (
                                <li key={f.name}
                                  title={f.note || undefined}
                                  className="rounded-btn border border-border bg-surface px-1.5 py-0.5 font-mono text-[11px]">
                                  <span className="text-foreground">{f.name}</span>
                                  <span className="text-accent"> {f.unit}</span>
                                </li>
                              ))}
                            </ul>
                          </div>
                          {row.notes.length > 0 && (
                            <ul className="space-y-1 text-[11px] text-secondary">
                              {row.notes.map((n, i) => <li key={i}>· {n}</li>)}
                            </ul>
                          )}
                          <div className="flex flex-wrap items-center gap-3 text-[11px]">
                            <span className="text-secondary">来源依据 <span className="text-foreground">{row.source_basis}</span></span>
                            <button className="text-accent"
                              onClick={() => setQualityFor(qualityFor === row.id ? null : row.id)}>
                              {qualityFor === row.id ? '收起质量检查' : '运行质量检查'}
                            </button>
                          </div>
                          {qualityFor === row.id && (
                            <div className="rounded-btn border border-border bg-surface px-3 py-2">
                              {quality.isPending ? (
                                <span className="flex items-center gap-2 text-[11px] text-secondary">
                                  <Loader2 className="h-3 w-3 animate-spin" />检查中…
                                </span>
                              ) : quality.error ? (
                                <span role="alert" className="text-[11px] text-danger">
                                  {(quality.error as Error).message}
                                </span>
                              ) : quality.data ? (
                                <CoverageDetail report={quality.data} />
                              ) : null}
                            </div>
                          )}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2 text-secondary">{row.source}</td>
                    <td className="px-3 py-2 text-foreground">
                      {row.freshness?.latest_date ?? '—'}
                      {row.freshness?.settlement === 'intraday' && (
                        <span className="ml-1 text-warning">{SETTLEMENT_TEXT.intraday}</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-secondary">{row.freshness?.updated_at ?? '—'}</td>
                    <td className="px-3 py-2 text-foreground">{row.currency}</td>
                    <td className="px-3 py-2">
                      <span className={row.pit.usable ? 'text-foreground' : 'text-warning'}>
                        {PIT_TEXT[row.pit.mode] ?? row.pit.mode}
                      </span>
                      {row.pit.known_at_column && (
                        <span className="block font-mono text-[11px] text-secondary">
                          {row.pit.known_at_column}
                        </span>
                      )}
                    </td>
                    <td className={`px-3 py-2 ${fresh.className}`}>
                      {fresh.text}
                      {row.freshness && row.freshness.lag_sessions > 0 && (
                        <span className="block text-[11px]">落后 {row.freshness.lag_sessions} 个交易日</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {rows.some(r => r.freshness?.note) && (
        <ul className="space-y-1 text-[11px] text-secondary">
          {[...new Set(rows.map(r => r.freshness?.note).filter(Boolean))].map((n, i) => (
            <li key={i}>· {n}</li>
          ))}
        </ul>
      )}
    </section>
  )
}
