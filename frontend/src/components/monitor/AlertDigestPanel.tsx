import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import { AlertTriangle, Check, ChevronDown, ChevronRight, Info, Lightbulb, MinusCircle } from 'lucide-react'
import { api, type AlertDigestItem, type AlertDigestTier } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'

/** 分档 → 视觉。`theme_hint` 刻意最淡：弱关联推断不能和公告同等突出。 */
const TIER_STYLE: Record<AlertDigestTier, { border: string; bg: string; text: string; dot: string }> = {
  must_know: {
    border: 'border-danger/40',
    bg: 'bg-danger/[0.06]',
    text: 'text-danger',
    dot: 'bg-danger',
  },
  worth_notice: {
    border: 'border-warning/35',
    bg: 'bg-warning/[0.05]',
    text: 'text-warning',
    dot: 'bg-warning',
  },
  background: {
    border: 'border-border/60',
    bg: 'bg-surface/40',
    text: 'text-muted',
    dot: 'bg-muted',
  },
  theme_hint: {
    border: 'border-border/40',
    bg: 'bg-transparent',
    text: 'text-muted',
    dot: 'bg-muted/60',
  },
}

const RELATION_STYLE: Record<string, string> = {
  direct: 'bg-accent/10 text-accent border-accent/25',
  inferred: 'bg-elevated/60 text-muted border-border/50',
  unknown: 'bg-elevated/40 text-muted border-border/40',
}

function fmtTs(ts?: number | null): string {
  if (!ts) return '—'
  const d = new Date(ts)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 一条提醒：四问全部展开，缺字段如实显示而不是留白。 */
function DigestRow({ item }: { item: AlertDigestItem }) {
  const [open, setOpen] = useState(item.tier === 'must_know')
  const style = TIER_STYLE[item.tier] ?? TIER_STYLE.background
  const Icon = item.tier === 'must_know' ? AlertTriangle
    : item.tier === 'theme_hint' ? Lightbulb
    : item.tier === 'background' ? MinusCircle
    : Info

  const title = item.what_happened.title ?? '(无标题)'
  const symbolText = item.symbols.length ? item.symbols.join('、') : '全市场'

  return (
    <div className={cn('rounded-lg border px-2.5 py-2', style.border, style.bg)}>
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        className="flex w-full items-start gap-1.5 text-left cursor-pointer"
      >
        <span className={cn('mt-1 h-1.5 w-1.5 shrink-0 rounded-full', style.dot)} />
        <Icon className={cn('mt-[1px] h-3 w-3 shrink-0', style.text)} />
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-1">
            <span className={cn('rounded px-1 py-[1px] text-[9px] font-semibold', style.text)}>
              {item.tier_label}
            </span>
            <span className="text-[10px] text-muted">{symbolText}</span>
            {item.repeat_count > 1 && (
              <span className="rounded bg-elevated/60 px-1 text-[9px] text-muted">
                重复 {item.repeat_count} 次
              </span>
            )}
            {item.is_new_fact && (
              <span className="rounded bg-accent/10 px-1 text-[9px] font-medium text-accent">新</span>
            )}
            {item.risk_changed && (
              <span className="rounded bg-warning/15 px-1 text-[9px] font-medium text-warning">变化</span>
            )}
          </span>
          <span className="mt-0.5 block text-[11px] leading-snug text-foreground">{title}</span>
        </span>
        {open ? (
          <ChevronDown className="mt-1 h-3 w-3 shrink-0 text-muted" />
        ) : (
          <ChevronRight className="mt-1 h-3 w-3 shrink-0 text-muted" />
        )}
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.16 }}
            className="overflow-hidden"
          >
            <div className="mt-1.5 space-y-1.5 border-t border-border/40 pt-1.5 pl-3">
              {/* ① 发生了什么 + 来源和日期 */}
              <div>
                <span className="text-[9px] font-semibold tracking-wide text-muted">发生了什么</span>
                <div className="text-[10px] leading-relaxed text-secondary">
                  {item.what_happened.event_date && item.what_happened.event_date !== 'unknown' && (
                    <span className="mr-1">事件日 {item.what_happened.event_date};</span>
                  )}
                  <span className="mr-1">发布 {fmtTs(item.what_happened.published_at && item.what_happened.published_at !== 'unknown' ? Date.parse(item.what_happened.published_at) : item.last_ts)};</span>
                  <span>
                    来源{' '}
                    {item.what_happened.sources.length
                      ? item.what_happened.sources.slice(0, 3).join('、')
                      : '未提供'}
                  </span>
                  <span className="ml-1 text-muted">
                    （{item.what_happened.verification_label ?? '未核实'}）
                  </span>
                </div>
              </div>

              {/* ② 为什么与这只票相关 —— 直接还是推断 */}
              <div>
                <span className="text-[9px] font-semibold tracking-wide text-muted">为什么相关</span>
                <div className="flex flex-wrap items-center gap-1 text-[10px] leading-relaxed text-secondary">
                  <span
                    className={cn(
                      'rounded border px-1 text-[9px] font-medium',
                      RELATION_STYLE[item.why_related.relation] ?? RELATION_STYLE.unknown,
                    )}
                  >
                    {item.why_related.relation_label}
                  </span>
                  <span>{item.why_related.relation_reason}</span>
                </div>
                {item.why_related.mapping_levels.length > 0 && (
                  <div className="text-[9px] text-muted">
                    映射等级 {item.why_related.mapping_levels.join('/')} · 方向 {item.why_related.direction}
                  </div>
                )}
              </div>

              {/* ③ 可能影响业务的渠道 */}
              <div>
                <span className="text-[9px] font-semibold tracking-wide text-muted">影响渠道</span>
                <div
                  className={cn(
                    'text-[10px] leading-relaxed',
                    item.impact_channel.available ? 'text-secondary' : 'text-muted italic',
                  )}
                >
                  {item.impact_channel.channel_text}
                </div>
                {item.impact_channel.note && (
                  <div className="text-[9px] leading-relaxed text-muted/80">{item.impact_channel.note}</div>
                )}
              </div>

              {/* ④ 要不要动作 */}
              <div className="flex items-start gap-1">
                <span
                  className={cn(
                    'shrink-0 rounded px-1 text-[9px] font-semibold',
                    item.action.needs_action
                      ? 'bg-danger/15 text-danger'
                      : 'bg-elevated/60 text-muted',
                  )}
                >
                  {item.action.label}
                </span>
                <span className="text-[10px] leading-relaxed text-muted">{item.action.hint}</span>
              </div>

              <div className="text-[9px] text-muted/80">为何是这个档：{item.tier_reason}</div>
              {item.what_happened.missing.length > 0 && (
                <div className="text-[9px] text-muted/80">
                  采集侧缺字段：{item.what_happened.missing.join('、')} —— 这几项没显示是没数据，不是没问题
                </div>
              )}
              {item.change_note && (
                <div className="text-[9px] text-warning">变化：{item.change_note}</div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

/**
 * 重要提醒面板 —— 告警「少而重要」。
 *
 * 只说三件事：**有几个值得看的、它们分别属于哪一档、每条的四个问题**。
 * 弱关联推断（`theme_hint`）默认折叠，且措辞明确为"线索"，
 * 不会与公司公告并列 —— 这正是宏柏新材那条风电告警应有的位置。
 */
export function AlertDigestPanel({ symbol, maxItems = 12 }: { symbol?: string; maxItems?: number }) {
  const [expanded, setExpanded] = useState(true)
  const [includeHints, setIncludeHints] = useState(false)
  const qc = useQueryClient()

  const q = useQuery({
    queryKey: QK.alertsDigest(symbol),
    queryFn: () => api.alertsDigest({ days: 7, limit: 2000, symbol, maxItems }),
    refetchInterval: 60_000,
  })

  // 「已看过」是落盘的（data/user_data/alert_seen.json）。只有用户点这个按钮
  // 才 ack —— 轮询本身绝不能改状态，否则第一次拉完就把所有条目标成看过，
  // 用户根本没机会看到。
  const ack = useMutation({
    mutationFn: () => api.alertsDigest({ days: 7, limit: 2000, symbol, maxItems, ack: true }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: QK.alertsDigest(symbol) }) },
  })

  const data = q.data
  const items = data?.items ?? []
  const visible = includeHints ? items : items.filter(i => i.tier !== 'theme_hint')
  const hintCount = items.filter(i => i.tier === 'theme_hint').length
  const counts = data?.counts

  return (
    <section className="rounded-xl border border-border bg-surface/40 shadow-lg shadow-black/5">
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        className="flex w-full items-center gap-2 border-b border-border/60 bg-surface/60 px-3 py-2 text-left cursor-pointer"
      >
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5 text-accent" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 text-accent" />
        )}
        <Lightbulb className="h-3.5 w-3.5 text-accent" />
        <h2 className="text-xs font-semibold text-foreground whitespace-nowrap">重要提醒</h2>
        {counts && (
          <span className="flex items-center gap-1 text-[10px] text-muted">
            {counts.by_tier.must_know > 0 && (
              <span className="rounded bg-danger/10 px-1 font-medium text-danger">
                需要知道 {counts.by_tier.must_know}
              </span>
            )}
            {counts.by_tier.worth_notice > 0 && (
              <span className="rounded bg-warning/10 px-1 font-medium text-warning">
                值得留意 {counts.by_tier.worth_notice}
              </span>
            )}
            {hintCount > 0 && (
              <span className="rounded bg-elevated/60 px-1">线索 {hintCount}</span>
            )}
            <span className="hidden sm:inline">
              （去重后 {counts.items}/{counts.after_dedupe} 条）
            </span>
          </span>
        )}
        <span className="ml-auto text-[9px] text-muted">
          {q.isFetching ? '刷新中…' : '按事件去重 · 只说新事实'}
        </span>
      </button>

      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18 }}
            className="overflow-hidden"
          >
            <div className="space-y-1.5 p-2.5">
              {q.isPending && <div className="py-3 text-center text-[10px] text-muted">加载中…</div>}
              {q.isError && (
                <div className="py-3 text-center text-[10px] text-danger">提醒摘要加载失败</div>
              )}
              {!q.isPending && !q.isError && visible.length === 0 && (
                <div className="py-4 text-center text-[10px] text-muted">
                  最近 7 天没有需要你处理的新事实。
                  {hintCount > 0 && ` 另有 ${hintCount} 条主题线索，默认不显示。`}
                </div>
              )}
              {visible.map((item, i) => (
                <DigestRow key={item.event_group_id ?? `${item.last_ts}-${i}`} item={item} />
              ))}
              {hintCount > 0 && (
                <button
                  type="button"
                  onClick={() => setIncludeHints(v => !v)}
                  className="w-full rounded-md border border-border/40 py-1 text-[10px] text-muted transition-colors hover:border-accent/30 hover:text-accent cursor-pointer"
                >
                  {includeHints ? '收起主题线索' : `显示 ${hintCount} 条主题线索（弱关联，仅作参考）`}
                </button>
              )}
              {data && data.limitations.length > 0 && (
                <details className="rounded-md bg-elevated/30 px-2 py-1">
                  <summary className="cursor-pointer text-[9px] text-muted">
                    口径与限制（{data.limitations.length}）
                  </summary>
                  <ul className="mt-1 space-y-0.5 pl-3 text-[9px] leading-relaxed text-muted">
                    {data.limitations.map((l, i) => (
                      <li key={i} className="list-disc">{l}</li>
                    ))}
                  </ul>
                </details>
              )}
              <button
                type="button"
                disabled={ack.isPending}
                onClick={() => ack.mutate()}
                className="flex w-full items-center justify-center gap-1 rounded-md border border-border/40 py-1 text-[10px] text-muted transition-colors hover:border-accent/30 hover:text-accent disabled:opacity-50 cursor-pointer"
              >
                <Check className="h-2.5 w-2.5" />
                {ack.isPending
                  ? '记录中…'
                  : ack.isError
                    ? '记录失败，可重试'
                    : `这些我看过了（${counts?.items ?? 0} 条，下次不再提醒）`}
              </button>
              <div className="pt-0.5 text-[9px] leading-relaxed text-muted/70">{data?.note}</div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  )
}
