import { useState, useRef, useEffect, useMemo, useCallback } from 'react'
import { useNavigate, useSearchParams, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { AlertTriangle, RadioTower, Plus, Trash2, Settings2, Zap, Bell, ListChecks, BellRing, TrendingUp, Flame, Tags, ChevronDown, ChevronRight, ExternalLink } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { Skeleton } from '@/components/data/Skeleton'
import { api, type MonitorRule, type AlertEvent, type MonitorExtFieldItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtPrice, fmtPct, priceColorClass } from '@/lib/format'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { cn } from '@/lib/cn'
import { cnSignal } from '@/lib/signals'
import { useCustomSignalNames } from '@/lib/useCustomSignalNames'
import { LEGACY_STRATEGY_NOTIFY_EVENTS, STRATEGY_NOTIFY_EVENT_OPTIONS, strategyEventMeta, strategyName } from '@/lib/strategyMonitorEvents'
import { resolveWatchlistGroupColor } from '@/lib/watchlist-group-colors'
import { markSeen, resetBadge, leaveMonitorPage } from '@/lib/monitorBadge'
import { RuleEditor } from '@/components/monitor/RuleEditor'
import { AlertDigestPanel } from '@/components/monitor/AlertDigestPanel'
import { AlertRelationBadges } from '@/components/monitor/AlertRelationBadges'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { toNavItems, type NavItem } from '@/lib/listNav'
import { DimensionMembersDialog, type DimensionKind, type DimensionMembersTarget } from '@/components/DimensionMembersDialog'
import { usePreferences, useQuoteStatus } from '@/lib/useSharedQueries'

const TYPE_LABEL: Record<string, string> = {
  signal: '信号', price: '价格/涨跌', market: '市场异动', strategy: '策略监控', sector: '板块监控',
  abnormal: '异动监控', volume_delta: '轮询放量', date: '日期提醒',
  // 内置探测器（不是用户规则，见 P4「内置监控」块）。缺这三项时页面上会直接漏出英文
  // source，筛选 tab 也点不出东西 —— 实测 49 条告警里 40 条属于这一类。
  policy_radar: '政策雷达', policy_calendar: '政策日历', paper: '模拟盘',
}

/**
 * 触发记录筛选 tab。
 *
 * 必须与后端实际会出现的 `source` 对齐：实测 49 条里 policy_radar 36 / paper 9 /
 * policy_calendar 4，而原来的 9 个 tab 没有一个能命中它们 —— 点「信号」「价格」
 * 「市场异动」「板块」全是空的。这里按「用户会问的问题」重排，并保留全部旧 tab
 * 以兼容既有用户规则。
 */
const FILTER_TABS = [
  'all', 'policy_radar', 'policy_calendar', 'paper',
  'strategy', 'signal', 'price', 'sector', 'abnormal', 'volume_delta', 'date', 'market',
] as const
type FilterTab = typeof FILTER_TABS[number]


/** 严重级别 → 左侧色条 + 图标 */
const SEVERITY_CONFIG: Record<string, { bar: string; icon: any; iconCls: string }> = {
  info:     { bar: 'bg-accent/40',       icon: Bell,        iconCls: 'text-accent' },
  warn:     { bar: 'bg-warning',          icon: TrendingUp,  iconCls: 'text-warning' },
  critical: { bar: 'bg-danger',           icon: Flame,       iconCls: 'text-danger' },
}
const SOURCE_BADGE_STYLE: Record<string, string> = {
  strategy: 'bg-amber-400/10 text-amber-400 border-amber-400/20',
  signal:   'bg-accent/10 text-accent border-accent/20',
  price:    'bg-emerald-400/10 text-emerald-400 border-emerald-400/20',
  market:   'bg-purple-500/10 text-purple-400 border-purple-500/20',
  sector:   'bg-cyan-500/10 text-cyan-700 border-cyan-500/20 dark:text-cyan-300',
  abnormal: 'bg-orange-500/10 text-orange-500 border-orange-500/20 dark:text-orange-400',
  volume_delta: 'bg-rose-500/10 text-rose-400 border-rose-500/20 dark:text-rose-300',
  date:     'bg-violet-500/10 text-violet-500 border-violet-500/20 dark:text-violet-300',
  // 内置探测器缺色标时全部退化成一坨灰，40/49 条告警受影响。
  policy_radar: 'bg-indigo-500/10 text-indigo-500 border-indigo-500/20 dark:text-indigo-300',
  policy_calendar: 'bg-teal-500/10 text-teal-600 border-teal-500/20 dark:text-teal-300',
  paper: 'bg-slate-500/10 text-slate-500 border-slate-500/20 dark:text-slate-300',
}

/** 事件方向 → 独立红绿标。这是整页最该一眼看到的词，不能只埋在 message 文本里。 */
const DIRECTION_STYLE: Record<string, string> = {
  '利好': 'bg-danger/10 text-danger border-danger/25',
  '利空': 'bg-bear/10 text-bear border-bear/25',
  '中性': 'bg-elevated text-muted border-border/50',
}



/**
 * 从事件行中取出 ext 字段标签 (行业/概念), 按 item 配置裁剪 (maxTags/hiddenIndices)。
 */
function getExtTags(ev: Record<string, unknown>, item: MonitorExtFieldItem | null): string[] {
  if (!item?.field) return []
  const key = item.field.replace('.', '__')
  const v = ev[key]
  if (v == null) return []
  const str = String(v)
  if (!str) return []
  let tags = str.split(/[、,，;；\-]/).map(s => s.trim()).filter(Boolean)
  const maxTags = item.maxTags ?? 0
  if (maxTags > 0) tags = tags.slice(0, maxTags)
  const hidden = item.hiddenIndices
  if (hidden?.length) tags = tags.filter((_, i) => !hidden.includes(i))
  return tags
}

/** 个股通知的 ext 标签行 (行业/概念), 无数据返回 null */

// ── P1 事件聚合 ──────────────────────────────────────────────────────────

/** 一个事件 → 一行。同一条新闻命中 4 只票时不再重复展示 4 遍。 */
interface EventGroup {
  key: string
  /** 事件标题（优先 event_title，回退 message 首段） */
  title: string
  direction: string | null
  horizon: string | null
  sectors: string[]
  /** 组内最严重的 severity */
  severity: string
  source: string
  sourceLabel: string
  latestTs: number
  /** 受影响标的 + 各自映射依据 */
  hits: {
    symbol: string
    name: string | null
    mappingLevel: string | null
    mappingBasis: string | null
    directness: string | null
    message: string
    ts: number
    /** 原始告警行：价格、命中信号、规则条件、关系角标都从它取，聚合不能丢这些 */
    raw: AlertEvent
  }[]
  /** 组内所有命中的信号名（去重，保持出现顺序） */
  signals: string[]
  /** 因果链（组内第一条有值的） */
  impactPath: string | null
  uncertainty: string | null
  verificationNote: string | null
  verificationLabel: string | null
  sourceUrl: string | null
  sourceName: string | null
  publishedAt: string | null
  repeatCount: number
}

const SEVERITY_RANK: Record<string, number> = { critical: 3, warn: 2, info: 1 }

/**
 * 把扁平告警列表按 `event_group_id || event_id || ts` 聚合成事件。
 *
 * 实测 49 条告警里有 19 个事件组、36 条 policy_radar —— 一条新闻被同一条规则对
 * 4 只持仓各发一次。用户必须连读 4 遍同一标题才能发现「这是一件事影响 4 只票」，
 * 而 critical 也被稀释成「同一件事 4 条」。
 */
function groupAlertsByEvent(events: AlertEvent[]): EventGroup[] {
  const map = new Map<string, EventGroup>()
  for (const ev of events) {
    const key = ev.event_group_id || ev.event_id || `ts:${ev.ts}:${ev.symbol ?? ''}`
    let g = map.get(key)
    if (!g) {
      g = {
        key,
        title: ev.event_title || ev.message || '(无标题)',
        direction: (ev.event_direction as string) ?? null,
        horizon: (ev.event_horizon as string) ?? null,
        sectors: Array.isArray(ev.event_sectors) ? (ev.event_sectors as string[]) : [],
        severity: ev.severity ?? 'info',
        source: ev.source,
        sourceLabel: TYPE_LABEL[ev.source] ?? ev.source,
        latestTs: ev.ts,
        hits: [],
        impactPath: null,
        uncertainty: null,
        verificationNote: null,
        verificationLabel: null,
        sourceUrl: null,
        sourceName: null,
        publishedAt: null,
        repeatCount: 0,
        signals: [],
      }
      map.set(key, g)
    }
    g.repeatCount += 1
    for (const sig of ev.signals ?? []) {
      if (!g.signals.includes(sig)) g.signals.push(sig)
    }
    // 组内取最严重的 severity：一件事只要有一条 critical，整组就是 critical。
    if ((SEVERITY_RANK[ev.severity ?? 'info'] ?? 0) > (SEVERITY_RANK[g.severity] ?? 0)) {
      g.severity = ev.severity ?? 'info'
    }
    if (ev.ts > g.latestTs) g.latestTs = ev.ts
    if (!g.direction && ev.event_direction) g.direction = ev.event_direction as string
    if (!g.horizon && ev.event_horizon) g.horizon = ev.event_horizon as string
    if (!g.impactPath && ev.impact_path_text) g.impactPath = ev.impact_path_text
    if (!g.uncertainty && ev.uncertainty) g.uncertainty = ev.uncertainty
    // verification_note 不在 AlertEvent 的显式字段里（走索引签名，类型是 unknown），
    // 但后端实测 36/49 条都带它 —— 是判断「该给这件事多少权重」的关键，照实取字符串。
    if (!g.verificationNote && typeof ev.verification_note === 'string' && ev.verification_note) {
      g.verificationNote = ev.verification_note
    }
    if (!g.verificationLabel && ev.verification_label) g.verificationLabel = ev.verification_label
    if (!g.sourceUrl && ev.source_url) g.sourceUrl = ev.source_url
    if (!g.sourceName && ev.source_name) g.sourceName = ev.source_name
    if (!g.publishedAt && ev.published_at) g.publishedAt = ev.published_at
    g.hits.push({
      symbol: ev.symbol ?? '',
      name: ev.name || null,
      mappingLevel: (ev.mapping_level as string) ?? null,
      mappingBasis: ev.mapping_basis ?? null,
      directness: (ev.directness as string) ?? null,
      message: ev.message ?? '',
      ts: ev.ts,
      raw: ev,
    })
  }
  return [...map.values()].sort(
    (a, b) => (SEVERITY_RANK[b.severity] ?? 0) - (SEVERITY_RANK[a.severity] ?? 0)
      || b.latestTs - a.latestTs,
  )
}

/**
 * 把告警 message 里已经作为组标题展示过的部分去掉。
 *
 * 后端 message 形如「【强关联】▲ 利好·政策 | 两部门印发"十五五"… | 储能方案拉动…」，
 * 标题是中间那段。组标题已经单独渲染，明细行再重复一遍会让同一句话在屏幕上出现 5 次
 * （实测 4 只票 + 标题本身）。
 */
function stripEventTitle(message: string, title: string): string {
  if (!title || title === message) return message === title ? '' : message
  const idx = message.indexOf(title)
  if (idx < 0) return message
  // 去掉标题及其紧邻的分隔符，保留前后有信息量的片段
  const before = message.slice(0, idx).replace(/[|｜·\-—>\s]+$/, '').trim()
  const after = message.slice(idx + title.length).replace(/^[|｜·\-—>\s]+/, '').trim()
  const kept = [before, after].filter(Boolean).join(' · ')
  return kept || ''
}

function fmtClock(ts: number): string {
  return new Date(ts).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

/**
 * P0 首屏结论带 —— 打开就知道「今天有几件事、几件要处理」。
 *
 * 原来首屏是「重要提醒」折叠块 + 一长列触发记录，而「重要提醒」在只有 theme_hint
 * 时会说「最近 7 天没有需要你处理的新事实」，同时下方触发记录里躺着 6 条 critical。
 * 这里用同一份 alerts 数据算出真实数量，不再让两个面板各说一套。
 */
function MonitorSummaryBand({
  events, total, rulesCount, groups, filter, onFilter,
}: {
  events: AlertEvent[]
  total: number
  rulesCount: number
  groups: EventGroup[]
  filter: FilterTab
  onFilter: (f: FilterTab) => void
}) {
  const critical = groups.filter(g => g.severity === 'critical')
  const warn = groups.filter(g => g.severity === 'warn')
  const policyGroups = groups.filter(g => g.source === 'policy_radar' || g.source === 'policy_calendar')
  const symbolSet = new Set(events.map(e => e.symbol).filter(Boolean))

  const headline = critical.length > 0
    ? `今天有 ${critical.length} 件事需要优先看`
    : warn.length > 0
      ? `今天有 ${warn.length} 件事值得留意`
      : groups.length > 0 ? '今天只有背景信息，没有需要处理的事' : '最近 7 天没有触发的告警'

  return (
    <section
      className="rounded-xl border border-border bg-surface/40 px-3.5 py-2.5"
      aria-label="监控结论"
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className={cn(
          'text-sm font-semibold',
          critical.length > 0 ? 'text-danger' : warn.length > 0 ? 'text-warning' : 'text-foreground',
        )}>
          {headline}
        </span>
        <span className="text-[11px] text-secondary">
          共 <span className="font-mono text-foreground">{groups.length}</span> 件事
          · 涉及 <span className="font-mono text-foreground">{symbolSet.size}</span> 只标的
          · 原始记录 <span className="font-mono text-foreground">{total}</span> 条
          {total > events.length && <span className="text-muted/70">（已加载 {events.length} 条）</span>}
        </span>
        {rulesCount === 0 && policyGroups.length > 0 && (
          <span className="ml-auto text-[10px] text-muted">
            这些告警来自内置探测器，不是你建的规则
          </span>
        )}
      </div>

      {/* 组内去重提示：一条新闻对 4 只票各发一次时，必须说清「已合并」 */}
      {groups.some(g => g.hits.length > 1) && (
        <p className="mt-0.5 text-[11px] text-muted">
          {groups.filter(g => g.hits.length > 1).length} 件事命中了多只标的，已合并展示 ——
          按钮里的数字是「受影响标的数」，不是「告警条数」。
        </p>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <button
          onClick={() => onFilter('all')}
          className={cn(
            'rounded-md border px-2 py-0.5 text-[10px] font-medium transition-colors cursor-pointer',
            filter === 'all' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border/60 text-muted hover:text-secondary',
          )}
        >
          全部 {groups.length}
        </button>
        {critical.length > 0 && (
          <span className="rounded-md border border-danger/30 bg-danger/[0.07] px-2 py-0.5 text-[10px] font-medium text-danger">
            需优先 {critical.length}
          </span>
        )}
        {warn.length > 0 && (
          <span className="rounded-md border border-warning/30 bg-warning/[0.06] px-2 py-0.5 text-[10px] font-medium text-warning">
            值得留意 {warn.length}
          </span>
        )}
        {policyGroups.length > 0 && (
          <button
            onClick={() => onFilter('policy_radar')}
            className={cn(
              'rounded-md border px-2 py-0.5 text-[10px] font-medium transition-colors cursor-pointer',
              filter === 'policy_radar'
                ? 'border-indigo-500/40 bg-indigo-500/10 text-indigo-500 dark:text-indigo-300'
                : 'border-border/60 text-muted hover:text-secondary',
            )}
          >
            政策类 {policyGroups.length}
          </button>
        )}
      </div>
    </section>
  )
}

function AlertExtTags({ ev, fields, onTagClick }: {
  ev: Record<string, unknown>
  fields: { concept: MonitorExtFieldItem | null; industry: MonitorExtFieldItem | null }
  onTagClick: (kind: DimensionKind, value: string, sourceField?: string) => void
}) {
  const conceptTags = getExtTags(ev, fields.concept)
  const industryTags = getExtTags(ev, fields.industry)
  if (conceptTags.length === 0 && industryTags.length === 0) return null
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1 pl-0.5">
      {industryTags.map((t, i) => (
        <button
          key={`i${i}`}
          onClick={event => { event.stopPropagation(); onTagClick('industry', t, fields.industry?.field) }}
          className="rounded bg-sky-500/10 px-1 py-px text-[9px] leading-tight text-sky-700 hover:brightness-95 dark:text-sky-400"
        >
          {t}
        </button>
      ))}
      {conceptTags.map((t, i) => (
        <button
          key={`c${i}`}
          onClick={event => { event.stopPropagation(); onTagClick('concept', t, fields.concept?.field) }}
          className="rounded bg-orange-500/10 px-1 py-px text-[9px] leading-tight text-orange-700 hover:brightness-95 dark:text-orange-400"
        >
          {t}
        </button>
      ))}
    </div>
  )
}

export function Monitor() {
  const qc = useQueryClient()
  const [editorOpen, setEditorOpen] = useState(false)
  const [editingRule, setEditingRule] = useState<MonitorRule | null>(null)
  const [editorPreset, setEditorPreset] = useState<Partial<MonitorRule> | null>(null)

  // 深链: /monitor?new=abnormal (异动监控页「告警规则」入口) → 直接弹出预置类型的编辑器
  const [searchParams, setSearchParams] = useSearchParams()
  useEffect(() => {
    const kind = searchParams.get('new')
    if (kind === 'abnormal') {
      setEditingRule(null)
      setEditorPreset({ type: 'abnormal', threshold_pct: 70, direction: 'both', abnormal_window: 'any', scope: 'all' })
      setEditorOpen(true)
      setSearchParams({}, { replace: true })
    }
  }, [searchParams, setSearchParams])

  // 触发记录: 过滤 + 统计 (提升到主组件, 供 header 行使用)
  const [filter, setFilter] = useState<FilterTab>('all')
  const [confirmClear, setConfirmClear] = useState(false)
  const [confirmClearRules, setConfirmClearRules] = useState(false)

  // 全局 ext 字段配置 (监控中心个股通知带行业/概念标签)
  const { data: prefs } = usePreferences()
  // 实时行情可用性: mode=none 表示当前生效数据源完全无法提供实时行情
  // (TickFlow 无有效 Key, 或路由源未就绪) — 监控/预警收不到最新价, 顶部提示去数据源配置。
  const { data: quoteStatus } = useQuoteStatus()
  const realtimeUnavailable = quoteStatus?.mode === 'none'
  const monitorExtFields = prefs?.monitor_ext_fields ?? {
    concept: { field: 'ext_gn_ths.所属概念' },
    industry: { field: 'ext_hy_ths.所属同花顺行业' },
  }
  const [extConfigOpen, setExtConfigOpen] = useState(false)
  const extColumnsParam = useMemo(() => {
    const parts = [monitorExtFields.concept?.field, monitorExtFields.industry?.field].filter(Boolean) as string[]
    return parts.length > 0 ? parts.join(',') : undefined
  }, [monitorExtFields])

  const alertsQuery = useQuery({
    queryKey: [...QK.alerts(filter === 'all' ? undefined : filter), extColumnsParam ?? ''],
    queryFn: () => api.alertsList({ days: 7, limit: 500, source: filter === 'all' ? undefined : filter, extColumns: extColumnsParam }),
    // 10s 轮询仅作 SSE strategy_alert 事件的兜底; 后台标签页不再拉 500 条全量。
    // 非交易时段无新告警可拉, 降为 120s 探活; is_trading_hours 字段缺失时保持原频率
    refetchInterval: () => (quoteStatus?.is_trading_hours === false ? 120_000 : 10_000),
    // 切筛选 tab 沿用上一份同 source 记录, 整表不闪没 (跨 source 不透传)
    placeholderData: (prev: any, prevQuery: any) => {
      const prevKey = prevQuery?.queryKey as readonly unknown[] | undefined
      return prevKey?.[1] === (filter === 'all' ? undefined : filter) ? prev : undefined
    },
  })
  const total = alertsQuery.data?.total ?? 0

  // 规则个数
  const rulesQuery = useQuery({ queryKey: QK.monitorRules, queryFn: api.monitorRulesList })
  const rulesCount = rulesQuery.data?.rules.length ?? 0
  const alertsRaw = (alertsQuery.data as { alerts?: AlertEvent[] } | undefined)?.alerts ?? []
  // P1：按事件聚合只在「全部」口径做 —— 切到具体来源时用户要的是原始条目。
  const eventGroups = useMemo(() => groupAlertsByEvent(alertsRaw), [alertsRaw])

  // 清除全部规则 (逐条删除)
  const clearRulesMut = useMutation({
    mutationFn: async () => {
      const rules = rulesQuery.data?.rules ?? []
      await Promise.all(rules.map(r => api.monitorRuleDelete(r.id)))
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      setConfirmClearRules(false)
    },
  })

  // 进入监控页: 清零未读徽标 + 记录"进入时刻", 之后新增的记录会闪烁
  // 离开监控页: 停止同步, 之后新增才计入未读
  const enterTsRef = useRef<number>(Date.now())
  useEffect(() => {
    enterTsRef.current = Date.now()
    markSeen()
    return () => leaveMonitorPage()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="flex flex-col h-full">
      <PageHeader title="监控中心" subtitle="实时信号与规则管理" />
      {realtimeUnavailable && (
        <div className="px-5 pb-1">
          <div className="mx-auto flex max-w-7xl items-center gap-2.5 rounded-xl border border-warning/30 bg-warning/[0.06] px-4 py-2.5">
            <AlertTriangle className="h-4 w-4 shrink-0 text-warning" />
            <span className="text-xs leading-relaxed text-secondary">
              实时行情当前不可用 — 监控与预警收不到最新价。可接入提供实时行情的数据源。
            </span>
            <Link
              to="/settings?tab=data-sources"
              className="ml-auto shrink-0 rounded-btn bg-warning/15 px-2.5 py-1 text-[11px] font-medium text-warning hover:bg-warning/25 transition-colors"
            >
              前往数据源配置
            </Link>
          </div>
        </div>
      )}
      <div className="flex-1 min-h-0 px-5 py-4">
        <div className="mx-auto flex h-full max-w-7xl flex-col gap-4 lg:flex-row">
          {/* 左栏: 结论带 + 重要提醒 + 触发记录 */}
          <div className="flex min-h-0 flex-1 flex-col gap-3">
            <MonitorSummaryBand
              events={alertsRaw}
              total={total}
              rulesCount={rulesCount}
              groups={eventGroups}
              filter={filter}
              onFilter={setFilter}
            />
            <AlertDigestPanel maxItems={12} />
            <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-border bg-surface/40 shadow-lg shadow-black/5">
            <div className="flex items-center gap-3 border-b border-border/60 bg-surface/60 px-4 py-2.5">
              <SectionHeader icon={BellRing} title="触发记录" />
              {/* 过滤标签 — 单行横向滚动, 不折行 (缩放/窄窗口下保持一行) */}
              <div className="flex min-w-0 flex-nowrap items-center gap-0.5 overflow-x-auto [&>button]:shrink-0">
                {FILTER_TABS.map(f => (
                  <button
                    key={f}
                    onClick={() => setFilter(f)}
                    className={cn(
                      'rounded-md px-1.5 py-0.5 text-[10px] font-medium transition-all cursor-pointer',
                      filter === f ? 'bg-accent/15 text-accent' : 'text-muted hover:bg-elevated/60 hover:text-secondary',
                    )}
                  >
                    {f === 'all' ? '全部' : TYPE_LABEL[f]}
                  </button>
                ))}
              </div>
              {/* 数量 + 清空 + 字段配置 */}
              <div className="ml-auto flex items-center gap-2 shrink-0">
                <button
                  onClick={() => setExtConfigOpen(true)}
                  title="配置行业/概念标签"
                  className={cn(
                    'inline-flex h-6 w-6 items-center justify-center rounded-lg border transition-all cursor-pointer',
                    extConfigOpen ? 'border-accent/40 text-accent' : 'border-border/60 bg-surface text-muted hover:border-accent/40 hover:text-accent',
                  )}
                >
                  <Tags className="h-3.5 w-3.5" />
                </button>
                <span className="rounded-md bg-elevated/50 px-1.5 py-0.5 text-[10px] font-medium text-muted">{total}</span>
                {total > 0 && (
                  <button
                    onClick={() => setConfirmClear(true)}
                    className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:bg-danger/10 hover:text-danger cursor-pointer"
                  >
                    <Trash2 className="h-2.5 w-2.5" />清空
                  </button>
                )}
              </div>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-3.5">
              <AlertsList alertsQuery={alertsQuery} confirmClear={confirmClear} setConfirmClear={setConfirmClear} total={total} enterTs={enterTsRef.current} monitorExtFields={monitorExtFields} />
            </div>
          </section>
          </div>

          {/* 右栏: 监控规则 */}
          <section className="flex min-h-0 w-full flex-col overflow-hidden rounded-xl border border-border bg-surface/40 shadow-lg shadow-black/5 lg:w-[400px] lg:shrink-0">
            <div className="flex items-center gap-3 border-b border-border/60 bg-surface/60 px-4 py-2.5">
              <SectionHeader icon={ListChecks} title="监控规则" />
              <span className="rounded-md bg-elevated/50 px-1.5 py-0.5 text-[10px] font-medium text-muted">{rulesCount}</span>
              <div className="ml-auto flex items-center gap-1">
                <button
                  onClick={() => { setEditingRule(null); setEditorPreset(null); setEditorOpen(true) }}
                  title="新建规则"
                  className="inline-flex h-6 w-6 items-center justify-center rounded-lg border border-border/60 bg-surface text-muted transition-all hover:border-accent/40 hover:text-accent hover:shadow-sm cursor-pointer"
                >
                  <Plus className="h-3.5 w-3.5" />
                </button>
                <button
                  onClick={() => setConfirmClearRules(true)}
                  disabled={rulesCount === 0}
                  title="清除全部规则"
                  className="inline-flex h-6 w-6 items-center justify-center rounded-lg border border-border/60 bg-surface text-muted transition-all hover:border-danger/40 hover:text-danger disabled:opacity-30 disabled:cursor-not-allowed cursor-pointer"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-3.5">
              <BuiltinMonitors events={alertsRaw} />
              <RulesList
                rulesQuery={rulesQuery}
                onEdit={(r) => { setEditingRule(r); setEditorOpen(true) }}
              />
            </div>
          </section>
        </div>
      </div>

      <RuleEditorDialog
        open={editorOpen}
        rule={editingRule}
        preset={editorPreset}
        onClose={() => { setEditorOpen(false); setEditingRule(null); setEditorPreset(null) }}
      />

      <ConfirmDialog
        open={confirmClearRules}
        title="清除全部监控规则?"
        message={`将删除全部 ${rulesCount} 条规则,此操作不可撤销。`}
        confirmText="清除"
        danger
        onCancel={() => setConfirmClearRules(false)}
        onConfirm={() => clearRulesMut.mutate()}
        pending={clearRulesMut.isPending}
      />

      <MonitorExtConfigDialog
        open={extConfigOpen}
        fields={monitorExtFields}
        onClose={() => setExtConfigOpen(false)}
      />
    </div>
  )
}

function SectionHeader({ icon: Icon, title }: { icon: any; title: string }) {
  return (
    <div className="flex items-center gap-1.5 shrink-0">
      <Icon className="h-4 w-4 text-accent" />
      <h2 className="text-sm font-semibold text-foreground whitespace-nowrap">{title}</h2>
    </div>
  )
}

// ── 触发记录列表 ──────────────────────────────────────
function AlertsList({ alertsQuery, confirmClear, setConfirmClear, total, enterTs, monitorExtFields }: {
  alertsQuery: ReturnType<typeof useQuery>
  confirmClear: boolean
  setConfirmClear: (v: boolean) => void
  total: number
  enterTs: number
  monitorExtFields: { concept: MonitorExtFieldItem | null; industry: MonitorExtFieldItem | null }
}) {
  const qc = useQueryClient()
  const [confirmTs, setConfirmTs] = useState<number | null>(null)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [previewEv, setPreviewEv] = useState<AlertEvent | null>(null)
  const [memberPreview, setMemberPreview] = useState<{ symbol: string; name?: string } | null>(null)
  const [previewNavList, setPreviewNavList] = useState<NavItem[]>([])
  const [dimensionTarget, setDimensionTarget] = useState<DimensionMembersTarget | null>(null)
  const customNames = useCustomSignalNames()

  const clearMut = useMutation({
    mutationFn: api.alertsClear,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['alerts'] }); setConfirmClear(false); resetBadge() },
  })
  const delMut = useMutation({
    mutationFn: (ts: number) => api.alertDelete(ts),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['alerts'] }),
  })

  // 点击删除: 第一次进入确认态, 第二次真删, 3 秒后自动复位
  const handleClickDelete = (ts: number) => {
    if (confirmTs === ts) {
      // 第二次点击 → 真删
      if (resetTimer.current) clearTimeout(resetTimer.current)
      setConfirmTs(null)
      delMut.mutate(ts)
    } else {
      // 第一次点击 → 进入确认态, 3 秒后自动复位
      setConfirmTs(ts)
      if (resetTimer.current) clearTimeout(resetTimer.current)
      resetTimer.current = setTimeout(() => setConfirmTs(null), 3000)
    }
  }

  const events = (alertsQuery.data as any)?.alerts ?? []

  // P1：来源筛选下按「事件」聚合，同一事件只占一行。
  // 聚合后才有空间放方向/因果链/映射依据 —— 扁平时每行只有 300px 宽，放不下。
  const groups = useMemo(() => groupAlertsByEvent(events), [events])

  // 事件行里要显示受影响的标的名称；告警自带的 name 常为空（实测 49 条里只有 43 条有），
  // 缺的用统一名称接口补齐，补齐失败也不影响代码本身可见。
  const alertSymbols = useMemo(
    () => [...new Set(events.map((e: AlertEvent) => e.symbol).filter(Boolean) as string[])].sort(),
    [events],
  )
  const namesQuery = useQuery({
    queryKey: QK.instrumentNames(alertSymbols),
    queryFn: () => api.instrumentNames(alertSymbols),
    enabled: alertSymbols.length > 0,
    staleTime: 300_000,
  })
  const names = namesQuery.data?.names ?? {}

  // 切股导航列表: 有 symbol 的触发记录 (按展示顺序)
  const alertsNavItems = useMemo(
    () => toNavItems(events.filter((ev: AlertEvent) => ev.symbol)),
    [events],
  )
  const handlePreviewEvent = useCallback((ev: AlertEvent) => {
    setPreviewEv(ev)
    setPreviewNavList(alertsNavItems)
  }, [alertsNavItems])
  // 弹窗内切股: 来自成分弹窗则更新 memberPreview, 否则按 symbol 找到对应事件 (保住 triggerInfo)
  const handleNavigate = useCallback((sym: string, name?: string) => {
    if (memberPreview) { setMemberPreview({ symbol: sym, name }); return }
    const ev = events.find((e: AlertEvent) => e.symbol === sym)
    if (ev) setPreviewEv(ev)
  }, [memberPreview, events])

  return (
    <div className="space-y-3">
      {alertsQuery.isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} h="h-14" rounded="rounded-card" />
          ))}
        </div>
      ) : alertsQuery.isError ? (
        <ErrorState
          title="触发记录加载失败"
          hint="告警接口暂时不可用，请重试；持续失败请检查数据源配置与网络。"
          retrying={alertsQuery.isFetching}
          onRetry={() => alertsQuery.refetch()}
        />
      ) : groups.length === 0 ? (
        <EmptyState
          icon={Bell}
          title="暂无触发记录"
          hint="监控规则命中后,触发记录会出现在这里。可在右侧配置规则,或在标的详情页加入监控。"
        />
      ) : (
        <div className="space-y-2">
          {groups.map((g, i) => (
            <EventCard
              key={g.key}
              group={g}
              index={i}
              isNew={g.latestTs > enterTs}
              names={names}
              customNames={customNames}
              onPreview={handlePreviewEvent}
              onDelete={handleClickDelete}
              confirmTs={confirmTs}
              deleting={delMut.isPending}
              monitorExtFields={monitorExtFields}
              onExtTagClick={(kind, value, sourceField) => {
                if (sourceField) setDimensionTarget({ kind, value, sourceField })
              }}
              onNavigateSymbol={(sym, name) => {
                const ev = events.find((e: AlertEvent) => e.symbol === sym)
                if (ev) handlePreviewEvent(ev)
                else setMemberPreview({ symbol: sym, name })
              }}
            />
          ))}
        </div>
      )}
      <ConfirmDialog
        open={confirmClear}
        title="清空全部触发记录?"
        message={`将删除全部 ${total} 条记录,此操作不可撤销。`}
        confirmText="清空"
        danger
        onCancel={() => setConfirmClear(false)}
        onConfirm={() => clearMut.mutate()}
        pending={clearMut.isPending}
      />

      <StockPreviewDialog
        symbol={memberPreview?.symbol ?? previewEv?.symbol ?? null}
        name={memberPreview?.name ?? previewEv?.name ?? undefined}
        triggerInfo={previewEv ? {
          price: previewEv.price ?? null,
          changePct: previewEv.change_pct ?? null,
          ts: previewEv.ts,
          signals: previewEv.signals,
          message: previewEv.message,
        } : null}
        navList={previewNavList}
        onNavigate={handleNavigate}
        onClose={() => { setPreviewEv(null); setMemberPreview(null); setPreviewNavList([]) }}
      />

      <DimensionMembersDialog
        target={dimensionTarget}
        onClose={() => setDimensionTarget(null)}
        onStockClick={(symbol, name, navList) => {
          setDimensionTarget(null)
          setMemberPreview({ symbol, name })
          setPreviewNavList(navList ?? alertsNavItems)
        }}
      />
    </div>
  )
}

// ── P1/P2 事件卡 ─────────────────────────────────────────

/**
 * 一个事件一行：标题只出现一次，下面平铺受影响标的 + 各自映射依据。
 *
 * 回答普通投资者的四个问题（与行动卡/组合风控同一套结构）：
 *   1. 什么事、利好还是利空 —— 独立的红绿方向标，不再埋进 message 文本
 *   2. 跟我哪只票有关、凭什么 —— mapping_basis（如「ETF成分股精确命中12只合计34.34%」）
 *   3. 怎么传过来的 —— impact_path_text 因果链
 *   4. 能不能信、有什么变数 —— verification_label + uncertainty + 原文链接
 */
function EventCard({
  group, index, isNew, names, customNames, onPreview, onDelete, confirmTs, deleting,
  monitorExtFields, onExtTagClick, onNavigateSymbol,
}: {
  group: EventGroup
  index: number
  isNew: boolean
  names: Record<string, string>
  customNames: Record<string, string>
  onPreview: (ev: AlertEvent) => void
  onDelete: (ts: number) => void
  confirmTs: number | null
  deleting: boolean
  monitorExtFields: { concept: MonitorExtFieldItem | null; industry: MonitorExtFieldItem | null }
  onExtTagClick: (kind: DimensionKind, value: string, sourceField?: string) => void
  onNavigateSymbol: (symbol: string, name?: string) => void
}) {
  const navigate = useNavigate()
  const [open, setOpen] = useState(group.hits.length <= 3 || group.severity === 'critical')
  void onPreview
  const sev = SEVERITY_CONFIG[group.severity] ?? SEVERITY_CONFIG.info
  const SevIcon = sev.icon
  const multi = group.hits.length > 1
  const directionCls = group.direction ? DIRECTION_STYLE[group.direction] : undefined

  return (
    <motion.div
      initial={isNew ? { opacity: 0, y: -8, scale: 0.98 } : { opacity: 0, y: 4 }}
      animate={isNew
        ? { opacity: [0, 1, 1, 0.85, 1], scale: [0.98, 1, 1, 1.01, 1], y: [-8, 0, 0, 0, 0] }
        : { opacity: 1, y: 0 }}
      transition={isNew
        ? { duration: 1.2, times: [0, 0.2, 0.5, 0.75, 1] }
        : { duration: 0.2, delay: Math.min(index * 0.02, 0.2) }}
      className={cn(
        'group relative overflow-hidden rounded-lg border bg-surface pl-3.5 pr-3 py-2.5 shadow-sm transition-all duration-200 hover:border-border hover:shadow-md hover:shadow-black/10',
        isNew ? 'border-accent/60 ring-1 ring-accent/30' : 'border-border/50',
      )}
    >
      <div className={cn('absolute left-0 top-0 h-full w-0.5', sev.bar)} />

      {/* 第一行：方向 + 标题 + 来源 + 时间 */}
      <div className="flex items-start gap-2">
        <SevIcon className={cn('mt-0.5 h-4 w-4 shrink-0', sev.iconCls)} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            {group.direction && (
              <span className={cn(
                'shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-semibold',
                directionCls ?? 'bg-elevated text-muted border-border/50',
              )}>
                {group.direction}
              </span>
            )}
            <span className={cn(
              'shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-medium',
              SOURCE_BADGE_STYLE[group.source] ?? 'bg-elevated text-muted border-border/50',
            )}>
              {group.sourceLabel}
            </span>
            {group.horizon && (
              <span className="shrink-0 rounded bg-elevated/60 px-1.5 py-0.5 text-[9px] text-muted">
                {group.horizon}
              </span>
            )}
            {multi && (
              <span className="shrink-0 rounded bg-accent/10 px-1.5 py-0.5 text-[9px] font-medium text-accent">
                影响 {group.hits.length} 只
              </span>
            )}
            {group.repeatCount > 1 && (
              <span className="shrink-0 rounded bg-elevated/60 px-1.5 py-0.5 text-[9px] text-muted">
                合并 {group.repeatCount} 条
              </span>
            )}
            {/* 策略类告警：策略名与进出场动作是核心语义，聚合后仍然要显示 */}
            {group.source === 'strategy' && (() => {
              const sname = strategyName(group.hits[0]?.raw.message ?? '')
              const meta = strategyEventMeta(group.hits[0]?.raw.type ?? '')
              return (
                <>
                  {sname && (
                    <span className={cn('shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-medium', SOURCE_BADGE_STYLE.strategy)}>
                      {sname}
                    </span>
                  )}
                  <span className={cn('shrink-0 text-[10px] font-medium', meta.className)}>{meta.action}</span>
                </>
              )
            })()}
          </div>

          <button
            type="button"
            onClick={() => setOpen(v => !v)}
            className="mt-1 flex w-full items-start gap-1 text-left cursor-pointer"
          >
            <span className="min-w-0 flex-1 text-[12px] font-medium leading-snug text-foreground">
              {group.title}
            </span>
            {open
              ? <ChevronDown className="mt-0.5 h-3 w-3 shrink-0 text-muted" />
              : <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 text-muted" />}
          </button>

          {/* 折叠态也要能看到「影响哪几只票」—— 这是决定要不要展开的唯一理由 */}
          {!open && (
            <div className="mt-1 flex flex-wrap gap-1">
              {group.hits.slice(0, 6).map(h => (
                <button
                  key={`${h.symbol}-${h.ts}`}
                  onClick={() => onNavigateSymbol(h.symbol, h.name ?? names[h.symbol])}
                  className="inline-flex items-center gap-1 rounded bg-elevated/50 px-1 py-px text-[10px] transition-colors hover:bg-elevated cursor-pointer"
                  title={`查看 ${h.symbol} 日K`}
                >
                  <span className="font-mono text-foreground">{h.symbol}</span>
                  {(h.name ?? names[h.symbol]) && (
                    <span className="text-muted">{h.name ?? names[h.symbol]}</span>
                  )}
                </button>
              ))}
              {group.hits.length > 6 && (
                <span className="text-[10px] text-muted">+{group.hits.length - 6}</span>
              )}
            </div>
          )}
        </div>
        <div className="shrink-0 text-right">
          <span className="block text-[10px] font-mono text-muted/60">{fmtClock(group.latestTs)}</span>
          {confirmTs === group.latestTs ? (
            <button
              onClick={() => onDelete(group.latestTs)}
              title="再次点击确认删除"
              className="mt-1 inline-flex items-center gap-1 rounded-md border border-danger/30 bg-danger/15 px-1.5 py-0.5 text-[10px] font-medium text-danger animate-pulse cursor-pointer"
            >
              <Trash2 className="h-2.5 w-2.5" />确认
            </button>
          ) : (
            <button
              onClick={() => onDelete(group.latestTs)}
              disabled={deleting}
              title="删除这条事件的最新记录"
              className="mt-1 rounded p-1 text-muted/0 transition-colors group-hover:text-muted/40 hover:!text-danger hover:bg-danger/10 cursor-pointer"
            >
              <Trash2 className="h-3 w-3" />
            </button>
          )}
        </div>
      </div>

      {/* 展开：因果链 / 各票映射依据 / 可信度 / 不确定性 */}
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.16 }}
            className="overflow-hidden"
          >
            <div className="mt-1.5 space-y-1.5 border-t border-border/40 pt-1.5 pl-6">
              {/* 受影响标的 + 各自映射依据 */}
              <div>
                <span className="text-[9px] font-semibold tracking-wide text-muted">影响哪些票</span>
                <div className="mt-0.5 space-y-0.5">
                  {group.hits.map(h => (
                    <div key={`${h.symbol}-${h.ts}`} className="flex items-start gap-1.5 text-[10px] leading-relaxed">
                      {/* 板块类告警：指数直接跳指数页，其余按成分股维度弹窗（沿用既有行为） */}
                      {h.raw.source === 'sector' ? (
                        <button
                          onClick={() => {
                            if (h.raw.sector_kind === 'index' && h.symbol) {
                              navigate(`/indices?symbol=${encodeURIComponent(h.symbol)}`)
                            } else if (h.raw.sector_source_field && h.raw.sector_value) {
                              onExtTagClick(
                                h.raw.sector_kind as DimensionKind,
                                h.raw.sector_value,
                                h.raw.sector_source_field,
                              )
                            }
                          }}
                          className="shrink-0 inline-flex items-center gap-1 text-foreground hover:text-accent cursor-pointer"
                          title={h.raw.sector_kind === 'index' ? '打开指数详情' : '查看成分股'}
                        >
                          <Tags className="h-2.5 w-2.5 text-cyan-600 dark:text-cyan-300" />
                          {h.symbol}
                        </button>
                      ) : (
                        <button
                          onClick={() => onNavigateSymbol(h.symbol, h.name ?? names[h.symbol])}
                          className="shrink-0 font-mono text-foreground hover:text-accent cursor-pointer"
                          title={`查看 ${h.symbol} 日K`}
                        >
                          {h.symbol}
                        </button>
                      )}
                      {(h.name ?? names[h.symbol]) && (
                        <span className="shrink-0 text-secondary">{h.name ?? names[h.symbol]}</span>
                      )}
                      {/* 价格/涨跌：策略与价格类告警必须带上，否则看不出是在什么价位触发的 */}
                      {h.raw.price != null && (
                        <span className={cn('shrink-0 font-mono', priceColorClass(h.raw.change_pct ?? 0))}>
                          {fmtPrice(h.raw.price)}
                          {h.raw.change_pct != null && ` ${fmtPct(h.raw.change_pct)}`}
                        </span>
                      )}
                      {h.mappingLevel && (
                        <span className="shrink-0 rounded bg-elevated/70 px-1 text-[9px] text-muted">
                          映射{h.mappingLevel}
                        </span>
                      )}
                      {h.directness && (
                        <span className="shrink-0 text-[9px] text-muted">{h.directness}</span>
                      )}
                      {h.mappingBasis && (
                        <span className="min-w-0 flex-1 text-muted">{h.mappingBasis}</span>
                      )}
                      {!h.mappingBasis && h.message && (
                        <span className="min-w-0 flex-1 text-muted">
                          {stripEventTitle(h.message, group.title)}
                        </span>
                      )}                    </div>
                  ))}
                </div>
              </div>

              {/* 命中信号：策略/信号类告警的核心信息，聚合后仍要保留 */}
              {group.signals.length > 0 && (
                <div className="flex flex-wrap items-center gap-1">
                  <span className="text-[9px] text-muted">命中</span>
                  {group.signals.map(sig => (
                    <span key={sig} className="rounded bg-accent/10 px-1.5 py-0.5 text-[9px] font-medium text-accent">
                      {cnSignal(sig, customNames)}
                    </span>
                  ))}
                </div>
              )}

              {/* 关联关系角标：只渲染组内第一条，且剥掉标题字段。
                  角标内部会再打印一次 event_title / impact_path / mapping_basis，
                  而这三项事件卡已单独展示；4 只票各渲染一遍会让同一句标题在屏幕上
                  出现 5 次 —— 这正是本次聚合要消除的重复。 */}
              {group.hits[0]?.raw && (
                <AlertRelationBadges
                  ev={{ ...group.hits[0].raw, event_title: null } as AlertEvent}
                />
              )}

              {/* 因果链：回答「怎么传到我这只票」 */}
              {group.impactPath && (
                <div>
                  <span className="text-[9px] font-semibold tracking-wide text-muted">传导路径</span>
                  <div className="text-[10px] leading-relaxed text-secondary">{group.impactPath}</div>
                </div>
              )}

              {/* 板块标签 */}
              {group.sectors.length > 0 && (
                <div className="flex flex-wrap items-center gap-1">
                  <span className="text-[9px] text-muted">涉及板块</span>
                  {group.sectors.slice(0, 6).map(s => (
                    <span key={s} className="rounded bg-cyan-500/10 px-1 py-px text-[9px] text-cyan-700 dark:text-cyan-300">
                      {s}
                    </span>
                  ))}
                </div>
              )}

              {/* 可信度 + 不确定性 + 原文：三个都影响「该给这件事多少权重」 */}
              <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[9px] leading-relaxed">
                {group.verificationLabel && (
                  <span className="text-muted">
                    核实 <span className="text-secondary">{group.verificationLabel}</span>
                  </span>
                )}
                {group.sourceName && (
                  <span className="text-muted">
                    来源 <span className="text-secondary">{group.sourceName}</span>
                  </span>
                )}
                {group.publishedAt && (
                  <span className="text-muted">发布 {group.publishedAt}</span>
                )}
                {group.sourceUrl && (
                  <a
                    href={group.sourceUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-0.5 text-accent hover:underline"
                  >
                    原文<ExternalLink className="h-2 w-2" />
                  </a>
                )}
              </div>
              {group.verificationNote && (
                <div className="text-[9px] leading-relaxed text-muted/80">{group.verificationNote}</div>
              )}
              {group.uncertainty && (
                <div className="text-[9px] leading-relaxed text-warning">不确定性：{group.uncertainty}</div>
              )}

              {/* ext 行业/概念标签（沿用既有配置，按组内第一条事件取） */}
              {group.hits[0]?.raw && (
                <AlertExtTags
                  ev={group.hits[0].raw as unknown as Record<string, unknown>}
                  fields={monitorExtFields}
                  onTagClick={onExtTagClick}
                />
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

// ── 监控规则列表 ──────────────────────────────────────

/**
 * P4 内置监控 —— 系统自带的探测器，不是用户规则。
 *
 * 实测：用户规则 0 条，却收到 40 条 policy_radar / policy_calendar 告警。右栏原来
 * 只显示「暂无监控规则」，用户完全看不到这些告警是谁发的、今天各发了多少。
 * 这里按 source 汇总真实触发数，并说明它们的性质（只读、不可编辑）。
 */
const BUILTIN_SOURCES: { source: string; desc: string }[] = [
  { source: 'policy_radar', desc: '扫财经快讯，按持仓/自选做行业与概念映射后告警' },
  { source: 'policy_calendar', desc: '按交易日历提前提醒已知的宏观与政策事项' },
]

function BuiltinMonitors({ events }: { events: AlertEvent[] }) {
  const stats = useMemo(() => {
    const m = new Map<string, { count: number; latest: number }>()
    for (const ev of events) {
      if (!BUILTIN_SOURCES.some(b => b.source === ev.source)) continue
      const cur = m.get(ev.source) ?? { count: 0, latest: 0 }
      cur.count += 1
      if (ev.ts > cur.latest) cur.latest = ev.ts
      m.set(ev.source, cur)
    }
    return m
  }, [events])

  if (stats.size === 0) return null

  return (
    <div className="mb-2.5 rounded-lg border border-border/50 bg-elevated/30 px-2.5 py-2">
      <div className="flex items-center gap-1 text-[10px] font-semibold text-secondary">
        <Zap className="h-3 w-3 text-indigo-500 dark:text-indigo-300" />
        内置监控（系统自带 · 只读）
      </div>
      <div className="mt-1 space-y-1">
        {BUILTIN_SOURCES.map(b => {
          const s = stats.get(b.source)
          if (!s) return null
          return (
            <div key={b.source} className="text-[10px] leading-relaxed">
              <div className="flex items-baseline gap-1.5">
                <span className={cn('rounded border px-1 py-px text-[9px] font-medium', SOURCE_BADGE_STYLE[b.source])}>
                  {TYPE_LABEL[b.source]}
                </span>
                <span className="font-mono text-foreground">{s.count}</span>
                <span className="text-muted">条 · 最近 {fmtClock(s.latest)}</span>
              </div>
              <div className="pl-0.5 text-[9px] text-muted/80">{b.desc}</div>
            </div>
          )
        })}
      </div>
      <p className="mt-1 text-[9px] leading-relaxed text-muted/70">
        这些告警不是你在下面建的规则发出的，删除或停用需要到「设置 → 实时监控」。
      </p>
    </div>
  )
}

function RulesList({ rulesQuery, onEdit }: {
  rulesQuery: ReturnType<typeof useQuery>
  onEdit: (rule: MonitorRule) => void
}) {
  const qc = useQueryClient()
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const customNames = useCustomSignalNames()

  const rules: MonitorRule[] = (rulesQuery.data as any)?.rules ?? []

  // 分组作用域规则: 拉取分组定义与成员, 展示分组名/成员数 chip (点击跳转自选页对应分组)
  const hasGroupRules = rules.some(r => r.scope === 'watchlist_group')
  const groupsQ = useQuery({
    queryKey: QK.watchlistGroups,
    queryFn: api.watchlistGroups,
    enabled: hasGroupRules,
  })
  const watchlistQ = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    enabled: hasGroupRules,
  })
  const groupMeta = useMemo(() => {
    const meta: Record<string, { name: string; color: string; count: number }> = {}
    for (const g of groupsQ.data?.groups ?? []) {
      meta[g.id] = { name: g.name, color: g.color, count: 0 }
    }
    for (const entry of watchlistQ.data?.symbols ?? []) {
      for (const gid of entry.group_ids ?? []) {
        if (meta[gid]) meta[gid].count += 1
      }
    }
    return meta
  }, [groupsQ.data, watchlistQ.data])

  // 收集所有规则的股票代码, 批量查名称
  const allSymbols = useMemo(() => {
    const set = new Set<string>()
    for (const r of rules) {
      if (r.scope === 'symbols') r.symbols.forEach(s => set.add(s))
    }
    return Array.from(set)
  }, [rules])
  const namesQuery = useQuery({
    queryKey: QK.instrumentNames(allSymbols),
    queryFn: () => api.instrumentNames(allSymbols),
    enabled: allSymbols.length > 0,
    staleTime: 300000,
  })
  const symbolNames = namesQuery.data?.names ?? {}

  // 切股导航列表: 个股规则 (取第一个 symbol, 按展示顺序)
  const rulesNavItems = useMemo(
    () => rules
      .filter(r => r.scope === 'symbols' && r.symbols.length > 0)
      .map(r => ({ symbol: r.symbols[0], name: symbolNames[r.symbols[0]] ?? undefined })),
    [rules, symbolNames],
  )

  const del = useMutation({
    mutationFn: api.monitorRuleDelete,
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.monitorRules }),
  })
  const toggleEnabled = (rule: MonitorRule) => {
    const { runtime_warning: _runtimeWarning, ...persistedRule } = rule
    api.monitorRuleSave({ ...persistedRule, enabled: !rule.enabled }).then(() =>
      qc.invalidateQueries({ queryKey: QK.monitorRules }),
    )
  }

  // 点击删除: 第一次进入确认态, 第二次真删, 3 秒后自动复位
  const handleClickDelete = (id: string) => {
    if (confirmId === id) {
      if (resetTimer.current) clearTimeout(resetTimer.current)
      setConfirmId(null)
      del.mutate(id)
    } else {
      setConfirmId(id)
      if (resetTimer.current) clearTimeout(resetTimer.current)
      resetTimer.current = setTimeout(() => setConfirmId(null), 3000)
    }
  }

  return (
    <div className="space-y-2.5">
      {rulesQuery.isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} h="h-16" rounded="rounded-card" />
          ))}
        </div>
      ) : rulesQuery.isError ? (
        <ErrorState
          title="监控规则加载失败"
          hint="规则接口暂时不可用，请重试；规则配置本身不受影响。"
          retrying={rulesQuery.isFetching}
          onRetry={() => rulesQuery.refetch()}
        />
      ) : rules.length === 0 ? (
        <EmptyState
          icon={RadioTower}
          title="暂无监控规则"
          hint="点击标题栏「+」新建规则,或在标的详情页点「加监控」快速添加。"
        />
      ) : (
        rules.map(r => {
          // 名称截取: "策略监控 · MACD金叉" → "MACD金叉", "信号监控 · 300750.SZ" → "信号监控"
          const dotIdx = r.name.indexOf(' · ')
          const displayName = dotIdx >= 0 ? r.name.slice(dotIdx + 3) : r.name
          return (
            <motion.div
              key={r.id}
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.2 }}
              className={cn(
                'group relative overflow-hidden rounded-lg border pl-3.5 pr-2.5 py-2 shadow-sm transition-all duration-200 hover:shadow-md hover:shadow-black/10',
                r.enabled
                  ? 'border-border/50 bg-surface hover:border-accent/30'
                  : 'border-border/30 bg-surface/40 opacity-70 hover:opacity-100',
              )}
            >
              {/* 左侧状态条 */}
              <div className={cn('absolute left-0 top-0 h-full w-0.5', r.enabled ? 'bg-accent/50' : 'bg-border')} />

              {/* 第一行: 分类标签 + 名称 + 操作按钮 */}
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  <span className={cn('shrink-0 rounded px-1.5 py-0.5 text-[9px] font-semibold', SOURCE_BADGE_STYLE[r.type] ?? 'bg-elevated text-muted')}>
                    {TYPE_LABEL[r.type]}
                  </span>
                  {r.lot_id && (
                    <span className="shrink-0 rounded px-1.5 py-0.5 text-[9px] font-semibold bg-emerald-400/10 text-emerald-500" title="由「持仓提醒」页托管, 请在持仓提醒页修改或删除">批次</span>
                  )}
                  {r.asset_type === 'index' && (
                    <span className="shrink-0 rounded px-1.5 py-0.5 text-[9px] font-semibold bg-sky-500/10 text-sky-400">指数</span>
                  )}
                  {/* 个股类型: 直接显示可点击的代码+名称; 分组类型: 分组chip跳自选页; 其他类型显示规则名 */}
                  {r.scope === 'symbols' && r.symbols.length > 0 ? (
                    <button
                      onClick={() => setPreviewSymbol(r.symbols[0])}
                      className="inline-flex items-center gap-1 min-w-0 hover:bg-elevated/50 rounded px-0.5 transition-colors cursor-pointer"
                      title={`查看 ${r.symbols[0]} 日K`}
                    >
                      <span className="font-mono text-xs font-medium text-foreground hover:text-accent">{r.symbols[0]}</span>
                      {symbolNames[r.symbols[0]] && <span className="text-xs text-secondary truncate">{symbolNames[r.symbols[0]]}</span>}
                    </button>
                  ) : r.scope === 'watchlist_group' && r.group_id ? (
                    (() => {
                      const meta = groupMeta[r.group_id]
                      if (!meta) {
                        return <span className="text-xs text-warning truncate" title={r.name}>分组已删除</span>
                      }
                      return (
                        <Link
                          to={`/watchlist?group=${r.group_id}`}
                          className="inline-flex min-w-0 items-center gap-1.5 rounded px-0.5 transition-colors hover:bg-elevated/50 cursor-pointer"
                          title={`「${meta.name}」分组 · 当前 ${meta.count} 只 · 点击查看分组`}
                        >
                          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${resolveWatchlistGroupColor(meta.color).dot}`} />
                          <span className="truncate text-xs font-medium text-foreground hover:text-accent">{meta.name}</span>
                          <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted">{meta.count}只</span>
                          <span className="shrink-0 text-[9px] text-muted/60">· 分组作用域</span>
                        </Link>
                      )
                    })()
                  ) : (
                    <h3 className={cn('text-xs font-medium truncate', r.enabled ? 'text-foreground' : 'text-muted')}>{displayName}</h3>
                  )}
                  {!r.enabled && <span className="shrink-0 text-[9px] text-secondary">· 停用</span>}
                </div>
                <div className="flex items-center gap-0.5 shrink-0">
                  {r.lot_id ? (
                    <span
                      className="inline-flex items-center rounded-md border border-border/60 bg-elevated/60 px-1.5 py-0.5 text-[9px] text-secondary"
                      title="由「持仓提醒」页生成的规则, 该页托管; 启停/修改/删除请到持仓提醒页"
                    >
                      批次托管
                    </span>
                  ) : (
                    <>
                      <button
                        onClick={() => toggleEnabled(r)}
                        title={r.enabled ? '停用' : '启用'}
                        className={cn(
                          'p-1 rounded-md transition-all cursor-pointer',
                          r.enabled ? 'text-accent hover:bg-accent/10' : 'text-muted hover:bg-elevated hover:text-accent',
                        )}
                      >
                        <Zap className="h-3.5 w-3.5" />
                      </button>
                      <button
                        onClick={() => onEdit(r)}
                        className="p-1 rounded-md text-secondary transition-all hover:bg-accent/10 hover:text-accent cursor-pointer"
                        title="编辑"
                      >
                        <Settings2 className="h-3.5 w-3.5" />
                      </button>
                      {confirmId === r.id ? (
                        <button
                          onClick={() => handleClickDelete(r.id)}
                          title="再次点击确认删除"
                          className="inline-flex items-center gap-1 rounded-md bg-danger/15 px-1.5 py-0.5 text-[9px] font-medium text-danger border border-danger/30 animate-pulse cursor-pointer"
                        >
                          <Trash2 className="h-2.5 w-2.5" />确认
                        </button>
                      ) : (
                        <button
                          onClick={() => handleClickDelete(r.id)}
                          disabled={del.isPending}
                          className="p-1 rounded-md text-secondary transition-all hover:bg-danger/10 hover:text-danger cursor-pointer"
                          title="删除"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </>
                  )}
                </div>
              </div>

              {r.runtime_warning && (
                <div className="mt-1 flex items-center gap-1 text-[9px] text-warning">
                  <AlertTriangle className="h-3 w-3 shrink-0" />
                  <span className="truncate" title={r.runtime_warning}>{r.runtime_warning}</span>
                </div>
              )}

              {/* 第二行: 类型摘要 */}
              {r.type === 'sector' ? (
                <div className="mt-1 flex min-w-0 flex-wrap items-center gap-1 pl-0.5">
                  {(r.sector_targets ?? []).slice(0, 3).map(target => (
                    <span key={target.key} className="max-w-28 truncate rounded bg-cyan-500/8 px-1.5 py-0.5 text-[9px] text-cyan-700 dark:text-cyan-300">
                      {target.name}
                    </span>
                  ))}
                  {(r.sector_targets?.length ?? 0) > 3 && (
                    <span className="text-[9px] text-muted">+{(r.sector_targets?.length ?? 0) - 3}</span>
                  )}
                  <span className="text-[9px] text-secondary">·</span>
                  <span className="text-[9px] text-secondary">
                    {r.sector_trigger === 'momentum' ? `${r.window_minutes ?? 5}分钟异动` : '涨跌幅'}
                    {r.direction === 'down' ? ' ≤ -' : ' ≥ '}{r.threshold_pct ?? 1}%
                  </span>
                </div>
              ) : r.type === 'abnormal' ? (
                <div className="mt-1 flex min-w-0 flex-wrap items-center gap-1 pl-0.5">
                  <span className="rounded bg-orange-500/8 px-1.5 py-0.5 text-[9px] text-orange-500 dark:text-orange-400">
                    接近度 ≥ {r.threshold_pct ?? 70}%
                  </span>
                  <span className="rounded bg-elevated px-1.5 py-0.5 text-[9px] text-secondary">
                    {r.abnormal_window && r.abnormal_window !== 'any' ? `${r.abnormal_window.toUpperCase()} 窗口` : '全部窗口'}
                  </span>
                  <span className="rounded bg-elevated px-1.5 py-0.5 text-[9px] text-secondary">
                    {r.direction === 'up' ? '涨势偏离' : r.direction === 'down' ? '跌势偏离' : '涨跌双向'}
                  </span>
                </div>
              ) : r.type === 'date' ? (
                <div className="mt-1 flex items-center gap-1 pl-0.5 text-[9px] text-secondary">
                  <span>提醒 {r.remind_date ?? ''}</span>
                  {(r.lead_days ?? 0) > 0 && <span>· 提前{r.lead_days}天</span>}
                  <span>· 仅交易日盘中评估</span>
                </div>
              ) : r.type === 'volume_delta' ? (
                <div className="mt-1 flex min-w-0 flex-wrap items-center gap-1 pl-0.5">
                  <span className="rounded bg-rose-500/8 px-1.5 py-0.5 text-[9px] font-mono text-rose-500 dark:text-rose-300">
                    {r.metric === 'amount'
                      ? `单轮增量 ≥ ${Math.round((r.threshold_amount ?? 1e6) / 1e4).toLocaleString()} 万元`
                      : `单轮增量 ≥ ${(r.threshold_volume ?? 9000).toLocaleString()} 手`}
                  </span>
                  <span className="rounded bg-elevated px-1.5 py-0.5 text-[9px] text-secondary">
                    冷却 {Math.round((r.cooldown_seconds ?? 300) / 60)} 分钟
                  </span>
                  {r.basic_filter && Object.values(r.basic_filter).some(v => v !== null && v !== false) && (
                    <span className="rounded bg-elevated px-1.5 py-0.5 text-[9px] text-secondary">
                      基础过滤{r.basic_filter.exclude_st ? ' · 剔除ST' : ''}
                    </span>
                  )}
                </div>
              ) : r.type === 'strategy' && r.strategy_id ? (
                <div className="mt-1 flex flex-wrap items-center gap-1 pl-0.5">
                  {(r.score_min != null || r.score_max != null) && (
                    <span className="rounded bg-amber-400/10 px-1.5 py-0.5 text-[9px] font-mono text-amber-500 dark:text-amber-300">
                      评分 {r.score_min ?? 0}–{r.score_max ?? 100}
                    </span>
                  )}
                  {(r.notify_events ?? LEGACY_STRATEGY_NOTIFY_EVENTS).map(event => {
                    const option = STRATEGY_NOTIFY_EVENT_OPTIONS.find(item => item.key === event)
                    return option ? (
                      <span key={event} className="rounded bg-elevated px-1.5 py-0.5 text-[9px] text-secondary">
                        {option.label}
                      </span>
                    ) : null
                  })}
                </div>
              ) : r.conditions.length > 0 && (
                <div className="mt-0.5 flex items-center gap-1 pl-0.5">
                  <span className="text-[9px] text-secondary shrink-0">条件</span>
                  <span className="min-w-0 flex flex-wrap items-center gap-x-1 gap-y-0.5 text-[9px]">
                    {r.conditions.slice(0, 3).map((c, i) => (
                      <span key={i} className="inline-flex items-center gap-0.5">
                        {i > 0 && <span className="text-secondary">{r.logic === 'and' ? '且' : '或'}</span>}
                        {c.op === 'truth' ? (
                          <span className="text-accent/80">{cnSignal(c.field, customNames)}</span>
                        ) : (
                          <span className="text-foreground/80 font-mono">{cnSignal(c.field, customNames)}{c.op}{c.value}</span>
                        )}
                      </span>
                    ))}
                    {r.conditions.length > 3 && <span className="text-secondary">+{r.conditions.length - 3}</span>}
                  </span>
                </div>
              )}
            </motion.div>
          )
        })
      )}

      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewSymbol ? symbolNames[previewSymbol] : undefined}
        navList={rulesNavItems}
        onNavigate={(sym) => setPreviewSymbol(sym)}
        onClose={() => setPreviewSymbol(null)}
      />
    </div>
  )
}

// ── 规则编辑对话框 ────────────────────────────────────
function RuleEditorDialog({ open, rule, preset, onClose }: {
  open: boolean
  rule: MonitorRule | null
  preset?: Partial<MonitorRule> | null
  onClose: () => void
}) {
  const backdrop = useDialogBackdrop(onClose)
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-50 flex items-start justify-center overflow-auto bg-black/40 backdrop-blur-sm p-4"
          {...backdrop}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: 8 }}
            transition={{ duration: 0.15 }}
            className="mt-4 w-full max-w-3xl"
            onClick={e => e.stopPropagation()}
          >
            <RuleEditor
              rule={rule}
              preset={preset ?? undefined}
              onClose={onClose}
              onSaved={onClose}
            />
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

// ── 确认对话框 ────────────────────────────────────────
function ConfirmDialog({ open, title, message, confirmText, danger, pending, onCancel, onConfirm }: {
  open: boolean
  title: string
  message: string
  confirmText?: string
  danger?: boolean
  pending?: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
          onClick={onCancel}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.96 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.96 }}
            transition={{ duration: 0.15 }}
            className="w-full max-w-sm rounded-2xl border border-border bg-surface p-5 shadow-2xl"
            onClick={e => e.stopPropagation()}
          >
            <h3 className="text-sm font-medium text-foreground">{title}</h3>
            <p className="mt-1.5 text-xs text-muted">{message}</p>
            <div className="mt-4 flex justify-end gap-2">
              <button onClick={onCancel} className="px-3 py-1.5 rounded-btn bg-elevated text-secondary text-xs cursor-pointer">取消</button>
              <button
                onClick={onConfirm}
                disabled={pending}
                className={cn(
                  'px-3 py-1.5 rounded-btn text-xs font-medium disabled:opacity-50 cursor-pointer',
                  danger ? 'bg-danger text-white' : 'bg-accent text-white',
                )}
              >
                {confirmText ?? '确定'}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

/** 监控中心 ext 字段配置弹窗: 选概念/行业字段, 保存到 preferences.monitor_ext_fields */
function MonitorExtConfigDialog({ open, fields, onClose }: {
  open: boolean
  fields: { concept: MonitorExtFieldItem | null; industry: MonitorExtFieldItem | null }
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [concept, setConcept] = useState<MonitorExtFieldItem | null>(fields.concept)
  const [industry, setIndustry] = useState<MonitorExtFieldItem | null>(fields.industry)
  useEffect(() => { setConcept(fields.concept); setIndustry(fields.industry) }, [fields.concept, fields.industry])

  const schema = useQuery({
    queryKey: QK.extDataSchemaAll,
    queryFn: api.extDataSchemaAll,
    enabled: open,
    staleTime: 60_000,
  })
  // 下拉选项: 按扩展表分组 → [{ group: 表名, options: [{value, label}] }]
  const groups = useMemo(() => {
    return (schema.data?.items ?? []).map(tbl => ({
      group: tbl.label || tbl.id,
      options: tbl.columns.map(col => ({
        value: `${tbl.id}.${col.name}`,
        label: col.label || col.name,
      })),
    }))
  }, [schema.data])

  const handleSave = async () => {
    await api.updateRealtimeMonitorConfig({ monitor_ext_fields: { concept, industry } })
    qc.invalidateQueries({ queryKey: QK.preferences })
    onClose()
  }

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
          onClick={onClose}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: 0.96 }}
            transition={{ duration: 0.15 }}
            className="w-full max-w-md rounded-2xl border border-border bg-surface p-5 shadow-2xl max-h-[85vh] overflow-y-auto"
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 mb-4">
              <Tags className="h-4 w-4 text-accent" />
              <h3 className="text-sm font-medium text-foreground">个股通知标签配置</h3>
            </div>
            <p className="text-[11px] text-muted mb-4">选择在触发记录和推送通知中显示的行业/概念字段,留空则不显示。</p>
            <div className="space-y-4">
              <ExtFieldSection label="行业字段" value={industry} onChange={setIndustry} groups={groups} loading={schema.isLoading} />
              <ExtFieldSection label="概念字段" value={concept} onChange={setConcept} groups={groups} loading={schema.isLoading} />
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button onClick={onClose} className="px-3 py-1.5 rounded-btn text-xs text-secondary hover:text-foreground transition-colors cursor-pointer">取消</button>
              <button onClick={handleSave} className="px-3 py-1.5 rounded-btn text-xs font-medium bg-accent text-white cursor-pointer">保存</button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

/** 单个 ext 字段配置区: 字段下拉 + 显示前N个 + 隐藏指定位置 */
function ExtFieldSection({ label, value, onChange, groups, loading }: {
  label: string
  value: MonitorExtFieldItem | null
  onChange: (v: MonitorExtFieldItem | null) => void
  groups: { group: string; options: { value: string; label: string }[] }[]
  loading: boolean
}) {
  const field = value?.field ?? ''
  const maxTags = value?.maxTags ?? 0
  const hidden = value?.hiddenIndices ?? []

  // 选/换字段时, 保留已有 maxTags/hiddenIndices 配置
  const pickField = (f: string | null) => {
    onChange(f ? { field: f, maxTags: value?.maxTags, hiddenIndices: value?.hiddenIndices } : null)
  }
  const setMaxTags = (n: number) => {
    onChange({ field, maxTags: n, hiddenIndices: n > 0 ? hidden.filter(i => i < n) : undefined })
  }
  const toggleHidden = (i: number) => {
    const next = hidden.includes(i) ? hidden.filter(x => x !== i) : [...hidden, i]
    onChange({ field, maxTags, hiddenIndices: next.length ? next : undefined })
  }

  return (
    <div className="space-y-2">
      <label className="text-xs text-secondary block">{label}</label>
      <div className="flex items-center gap-2">
        <select
          value={field}
          onChange={e => pickField(e.target.value || null)}
          disabled={loading}
          className="flex-1 min-w-0 h-8 bg-elevated border border-border rounded text-xs text-foreground px-2 focus:outline-none focus:border-accent/50"
        >
          <option value="">不显示</option>
          {groups.map(g => (
            <optgroup key={g.group} label={g.group}>
              {g.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
            </optgroup>
          ))}
        </select>
        {field && (
          <button onClick={() => onChange(null)} title="清除" className="shrink-0 p-1 rounded text-muted hover:text-danger transition-colors cursor-pointer">
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      {field && (
        <div className="flex items-center gap-2 pl-0.5">
          <span className="text-[10px] text-muted shrink-0">显示前N个</span>
          <input
            type="number" min={0} max={20}
            value={maxTags || ''}
            onChange={e => setMaxTags(e.target.value ? Number(e.target.value) : 0)}
            placeholder="不限"
            className="w-14 h-6 bg-elevated border border-border rounded text-[11px] text-foreground px-1.5 focus:outline-none focus:border-accent/50"
          />
          <span className="text-[10px] text-muted/60">留空=全部</span>
        </div>
      )}
      {field && maxTags > 0 && (
        <div className="flex items-center gap-2 pl-0.5">
          <span className="text-[10px] text-muted shrink-0">隐藏位置</span>
          <div className="flex flex-wrap gap-1">
            {Array.from({ length: maxTags }, (_, i) => (
              <button
                key={i}
                onClick={() => toggleHidden(i)}
                className={`w-5 h-5 rounded text-[10px] font-medium transition-colors cursor-pointer ${
                  hidden.includes(i) ? 'bg-elevated text-muted line-through' : 'bg-accent/15 text-accent'
                }`}
              >{i + 1}</button>
            ))}
          </div>
          <span className="text-[10px] text-muted/60">点数字划掉=隐藏该位置</span>
        </div>
      )}
    </div>
  )
}
