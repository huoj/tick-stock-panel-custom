import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, ChevronDown, Info, Plus, Search, Trash2 } from 'lucide-react'
import { api, type ActionCard as ActionCardData, type PositionPlanBatch } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { PageHeader } from '@/components/PageHeader'
import { Skeleton } from '@/components/data/Skeleton'
import { PlainGuide, ACTION_CARD_TERMS } from './PlainGuide'

/** 状态徽标: 三种状态必须一眼可辨 —— 这是"不把计划误认为持仓"的第一道防线。 */
const STATE_STYLE: Record<string, string> = {
  watch: 'bg-muted/15 text-muted',
  planning: 'bg-warning/15 text-warning',
  holding: 'bg-accent/15 text-accent',
}

function StateBadge({ state, label }: { state: string; label: string }) {
  return (
    <span className={cn('rounded px-2 py-0.5 text-[11px] font-medium', STATE_STYLE[state] ?? STATE_STYLE.watch)}>
      {label}
    </span>
  )
}

/** 分段标题 —— 三段必须视觉上分得开, 不能混成一段。 */
function SectionTitle({ tone, title, subtitle }: { tone: 'fact' | 'infer' | 'plan'; title: string; subtitle: string }) {
  const style = {
    fact: 'border-muted/25 bg-muted/5',
    infer: 'border-accent/25 bg-accent/5',
    plan: 'border-warning/25 bg-warning/5',
  }[tone]
  const Icon = { fact: CheckCircle2, infer: Info, plan: AlertTriangle }[tone]
  return (
    <div className={cn('flex items-start gap-2 rounded-btn border px-3 py-2', style)}>
      <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <div>
        <div className="text-xs font-medium text-foreground">{title}</div>
        <div className="text-[10px] leading-4 text-muted">{subtitle}</div>
      </div>
    </div>
  )
}

function PriceLine({ label, price, tone }: { label: string; price?: number | null; tone?: 'up' | 'down' }) {
  if (price == null) return null
  return (
    <div className="flex items-baseline justify-between gap-2 text-xs">
      <span className="text-secondary">{label}</span>
      <span className={cn('font-mono', tone === 'down' ? 'text-danger' : tone === 'up' ? 'text-success' : 'text-foreground')}>
        {price.toFixed(2)}
      </span>
    </div>
  )
}

/**
 * 首屏结论带 —— 不滚动就要能答出「什么票、多少钱、我赚多少」。
 *
 * 原来现价埋在事实段的双列网格里、浮盈亏再往里一层, 普通投资者第一眼只能
 * 看到五条 caveats 灰字。这里只做聚合, 不新增任何后端数据。
 */
function SummaryBand({ data }: { data: ActionCardData }) {
  const f = data.facts
  const h = f.holding
  const up = (f.change_pct ?? 0) >= 0
  return (
    <div className="rounded-btn border border-border/50 bg-elevated/40 px-3 py-2.5">
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
        <span className="text-lg font-medium text-foreground">{data.name || data.symbol}</span>
        <span className="font-mono text-xs text-muted">{data.symbol}</span>
        <StateBadge state={data.state} label={data.state_label} />
        <span className="ml-auto font-mono text-2xl font-semibold tabular-nums text-foreground">
          {f.price != null ? f.price.toFixed(2) : '—'}
        </span>
        <span className={cn('font-mono text-sm font-medium tabular-nums', up ? 'text-success' : 'text-danger')}>
          {up ? '▲ +' : '▼ '}{(f.change_pct ?? 0).toFixed(2)}%
        </span>
      </div>
      <div className="mt-1 flex flex-wrap items-baseline gap-x-4 gap-y-0.5 text-xs">
        {h ? (
          <>
            <span className="text-secondary">
              持 <span className="font-mono">{h.qty ?? '—'}</span> 股 · 成本{' '}
              <span className="font-mono">{h.avg_cost != null ? h.avg_cost.toFixed(2) : '—'}</span>
            </span>
            <span className={cn('font-mono font-medium', (h.pnl ?? 0) >= 0 ? 'text-success' : 'text-danger')}>
              浮盈亏 {h.pnl != null ? `${h.pnl.toLocaleString()} 元` : '—'}
              {h.pnl_pct != null ? `（${(h.pnl_pct * 100).toFixed(2)}%）` : ''}
            </span>
            {h.weight != null && <span className="text-muted">占账户 {(h.weight * 100).toFixed(1)}%</span>}
            {h.holding_days != null && <span className="text-muted">已持 {h.holding_days} 天</span>}
          </>
        ) : (
          <span className="text-muted">未持有这只票 —— 下面只有行情事实和系统推断，没有你的成本</span>
        )}
        <span className="ml-auto text-muted">截止 {data.as_of}</span>
      </div>
    </div>
  )
}

type LadderKind = 'resistance' | 'support' | 'invalid' | 'entry' | 'plan'

interface LadderRow {
  price: number
  labels: string[]
  kind: LadderKind
}

const KEY_LEVEL_LABEL: Record<string, string> = {
  high_20: '20日高', low_20: '20日低',
  high_60: '60日高', low_60: '60日低',
  high_120: '120日高', low_120: '120日低',
  high_250: '一年高', low_250: '一年低',
}

const KIND_ORDER: Record<LadderKind, number> = { invalid: 0, entry: 1, plan: 2, resistance: 3, support: 3 }

const KIND_TONE: Record<LadderKind, string> = {
  resistance: 'text-warning',
  support: 'text-success',
  invalid: 'text-danger font-medium',
  entry: 'text-accent',
  plan: 'text-secondary',
}

/**
 * 价格地图 —— 把散在四个区块里的「价格类字段」合成一根轴。
 *
 * 后端早就算好了 key_levels / boll / high_250 / low_250 / chip_densest_* 以及
 * 失效价、买点、计划批次价, 但前端此前一次都没渲染, 用户要在三段里来回滚才
 * 能拼出「上面压着什么、下面撑着什么、在哪买、跌到哪算错」。这里只做聚合展示。
 */
function buildLadder(data: ActionCardData): LadderRow[] {
  const f = data.facts
  const cur = f.price
  const rows: LadderRow[] = []
  const push = (price: number | null | undefined, label: string, kind: LadderKind) => {
    if (price == null || !Number.isFinite(price) || price <= 0) return
    rows.push({ price, labels: [label], kind })
  }

  for (const [k, v] of Object.entries(f.key_levels ?? {})) {
    push(v?.price, KEY_LEVEL_LABEL[k] ?? k, k.startsWith('high') ? 'resistance' : 'support')
  }
  push(f.high_250, '一年最高', 'resistance')
  push(f.low_250, '一年最低', 'support')
  push(f.boll?.upper, '布林上轨', 'resistance')
  push(f.boll?.lower, '布林下轨', 'support')
  for (const [k, v] of Object.entries(f.mas ?? {})) {
    push(v, k, v >= cur ? 'resistance' : 'support')
  }
  const chip = (c: ActionCardData['facts']['chip_densest_above'] | null | undefined) => {
    if (!c) return
    push(
      (c.low + c.high) / 2,
      `筹码密集区 ${c.low.toFixed(2)}~${c.high.toFixed(2)}（占 ${c.pct}%）`,
      c.low >= cur ? 'resistance' : 'support',
    )
  }
  chip(f.chip_densest_above)
  chip(f.chip_densest_below)
  for (const r of data.invalidation ?? []) push(r.price, `失效条件：${r.condition}`, 'invalid')
  for (const c of data.entry_conditions ?? []) push(c.price, `考虑买入：${c.condition}`, 'entry')
  ;(data.user_plan?.batches ?? []).forEach((b, i) => {
    push(b.price, `计划第 ${i + 1} 批买入${b.done ? '（已执行）' : ''}`, 'plan')
  })

  // 同一价位常被重复赋值（前高既是 key_levels 又是 failure 价）：合并成一行。
  const merged = new Map<string, LadderRow>()
  for (const r of rows) {
    const key = r.price.toFixed(2)
    const hit = merged.get(key)
    if (!hit) {
      merged.set(key, r)
      continue
    }
    if (!hit.labels.includes(r.labels[0])) hit.labels.push(r.labels[0])
    if (KIND_ORDER[r.kind] < KIND_ORDER[hit.kind]) hit.kind = r.kind
  }
  return [...merged.values()].sort((a, b) => b.price - a.price)
}

function PriceLadder({ data }: { data: ActionCardData }) {
  const cur = data.facts.price
  const rows = useMemo(() => buildLadder(data), [data])
  if (!Number.isFinite(cur) || rows.length === 0) return null
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-xs font-medium text-foreground">价格地图</span>
        <span className="text-[10px] text-muted">
          现价在中间：上面的都在压着，下面的都在撑着；百分比是离现价多远
        </span>
      </div>
      <div className="rounded-btn border border-border/50 px-2 py-1.5">
        {rows.map(r => {
          const dist = ((r.price - cur) / cur) * 100
          return (
            <div key={r.price.toFixed(2)} className="flex items-baseline gap-2 text-[11px] leading-5">
              <span className={cn('w-14 shrink-0 text-right font-mono tabular-nums', KIND_TONE[r.kind])}>
                {dist > 0 ? '+' : ''}{dist.toFixed(2)}%
              </span>
              <span className={cn('w-14 shrink-0 font-mono tabular-nums', KIND_TONE[r.kind])}>
                {r.price.toFixed(2)}
              </span>
              <span className="min-w-0 flex-1 text-muted" title={r.labels.join(' · ')}>
                {r.labels.join(' · ')}
              </span>
            </div>
          )
        })}
        <div className="flex items-baseline gap-2 border-t border-border/50 bg-accent/10 text-[11px] font-medium leading-5 text-accent">
          <span className="w-14 shrink-0 text-right font-mono">现价</span>
          <span className="w-14 shrink-0 font-mono tabular-nums">{cur.toFixed(2)}</span>
          <span className="min-w-0 flex-1">
            近一年 {data.facts.position_in_250_pct?.toFixed(0) ?? '—'}% 分位（0=最底 100=最高）
          </span>
        </div>
      </div>
    </div>
  )
}

type ForwardRow = { n: number; stat: ActionCardData['inference']['forward_stats'][string] }

function forwardRows(stats: ActionCardData['inference']['forward_stats']): ForwardRow[] {
  return Object.entries(stats ?? {})
    .map(([w, stat]) => ({ n: Number(w), stat }))
    .filter(r => Number.isFinite(r.n) && r.stat?.win_rate != null)
    .sort((a, b) => a.n - b.n)
}

/** 纯把上表数字转成一句人话, 不新增任何判断依据。 */
function summarizeForward(rows: ForwardRow[]): string | null {
  const scored = rows.filter(r => r.stat.edge_vs_baseline_pp != null)
  if (scored.length < 2) return null
  const [first, ...rest] = scored
  const edgeOf = (r: ForwardRow) => r.stat.edge_vs_baseline_pp ?? 0
  const meanOf = (r: ForwardRow) => r.stat.mean_pct ?? 0
  const span = `${scored[0].n}～${scored[scored.length - 1].n} 日`
  if (edgeOf(first) > 0 && rest.every(r => edgeOf(r) < 0)) {
    const allNeg = rest.every(r => meanOf(r) < 0)
    return `只有最短的 ${first.n} 日强于基准，${rest.map(r => `${r.n} 日`).join('、')}全部弱于基准${allNeg ? '、平均收益还是负的' : ''}——是「短期有惯性、拿久了没优势」的形态，别拿 ${first.n} 日的胜率去做长线决定。`
  }
  if (edgeOf(first) <= 0 && rest.every(r => edgeOf(r) > 0)) {
    return `最短的 ${first.n} 日弱于基准，但 ${rest.map(r => `${r.n} 日`).join('、')}反而强于基准——短期还在震，拉长看才占优。`
  }
  if (scored.every(r => edgeOf(r) > 0)) return `${span}全部强于基准，这个形态在这只票上各期一致偏强。`
  if (scored.every(r => edgeOf(r) < 0)) return `${span}全部弱于基准，这个形态在这只票上没有优势，不宜当作买入理由。`
  return null
}

/**
 * 前瞻统计表 —— 原来只渲染了「上涨概率 + 基准」, 漏掉 mean_pct 全是负数这一关键事实。
 * 实测 000333.SZ: 1 日 67% 看着偏多, 但 3/5/10 日胜率只有 33%、平均涨跌 −0.91/−0.59/−1.38%,
 * 只看第一行会把结论读反。这里补齐平均涨跌与相对基准两列, 并加条形对比。
 */
function ForwardStatsTable({ stats }: { stats: ActionCardData['inference']['forward_stats'] }) {
  const rows = useMemo(() => forwardRows(stats), [stats])
  const summary = useMemo(() => summarizeForward(rows), [rows])
  if (rows.length === 0) return null
  return (
    <div className="space-y-1">
      <div className="flex items-baseline gap-2 text-[10px] text-muted">
        <span className="w-10 shrink-0">期间</span>
        <span className="min-w-0 flex-1">上涨概率（竖线=同期基准）</span>
        <span className="w-16 shrink-0 text-right">平均涨跌</span>
        <span className="w-14 shrink-0 text-right">对基准</span>
      </div>
      {rows.map(({ n, stat }) => {
        const edge = stat.edge_vs_baseline_pp
        const better = (edge ?? 0) >= 0
        return (
          <div key={n} className="flex items-center gap-2 text-[11px]">
            <span className="w-10 shrink-0 font-mono text-secondary">{n} 日</span>
            <div className="flex min-w-0 flex-1 items-center gap-1.5">
              <div className="relative h-2 min-w-6 flex-1 rounded-sm bg-muted/15">
                <div
                  className={cn('h-2 rounded-sm', better ? 'bg-success/60' : 'bg-danger/60')}
                  style={{ width: `${Math.min(100, Math.max(0, stat.win_rate ?? 0))}%` }}
                />
                {stat.baseline_win_rate != null && (
                  <div
                    className="absolute -top-0.5 h-3 w-px bg-foreground/70"
                    style={{ left: `${Math.min(100, Math.max(0, stat.baseline_win_rate))}%` }}
                  />
                )}
              </div>
              <span className={cn('w-20 shrink-0 text-right font-mono tabular-nums', better ? 'text-success' : 'text-danger')}>
                {stat.win_rate}%
                <span className="text-muted"> vs {stat.baseline_win_rate ?? '—'}%</span>
              </span>
            </div>
            <span className={cn('w-16 shrink-0 text-right font-mono tabular-nums',
              (stat.mean_pct ?? 0) >= 0 ? 'text-success' : 'text-danger')}>
              {stat.mean_pct == null ? '—' : `${stat.mean_pct > 0 ? '+' : ''}${stat.mean_pct}%`}
            </span>
            <span className={cn('w-14 shrink-0 text-right font-mono tabular-nums', better ? 'text-success' : 'text-danger')}>
              {edge == null ? '—' : `${edge > 0 ? '+' : ''}${edge}pp`}
            </span>
          </div>
        )
      })}
      {summary && (
        <div className="border-t border-border/40 pt-1 text-[10px] leading-4 text-secondary">
          系统按上表自动归纳：{summary}
        </div>
      )}
    </div>
  )
}

/** 事实段: 只放可核对数值, 不做解释。 */
function FactsSection({ data }: { data: ActionCardData }) {
  const f = data.facts
  // 波动换算：普通投资者看得懂「一天晃两百块」, 看不懂「ATR14=2.06」。纯算术, 不新增数据。
  const dailyYuan = f.atr14 != null ? f.atr14 * 100 : null
  const monthlyBand = f.vol_20d_annual_pct != null ? f.vol_20d_annual_pct / Math.sqrt(12) : null
  return (
    <div className="space-y-2">
      <SectionTitle tone="fact" title={data.sections.facts} subtitle="都是行情里能查到的数，没有系统猜测，你可以自己核对" />
      <div className="rounded-btn border border-border/50 px-3 py-2.5 space-y-1.5">
        <div className="grid grid-cols-2 gap-x-4 gap-y-1">
          <div className="flex items-baseline justify-between gap-2 text-xs">
            <span className="text-secondary">一年分位</span>
            <span className={cn('font-mono',
              (f.position_in_250_pct ?? 0) >= 90 ? 'text-danger'
                : (f.position_in_250_pct ?? 0) <= 10 ? 'text-success' : 'text-foreground')}>
              {f.position_in_250_pct?.toFixed(0) ?? '—'}%
            </span>
          </div>
          <div className="flex items-baseline justify-between gap-2 text-xs">
            <span className="text-secondary">一年区间</span>
            <span className="font-mono text-[11px]">
              {f.low_250?.toFixed(2) ?? '—'} ~ {f.high_250?.toFixed(2) ?? '—'}
            </span>
          </div>
          <div className="flex items-baseline justify-between gap-2 text-xs">
            <span className="text-secondary">量比(20日)</span>
            <span className="font-mono">{f.volume_ratio_pct?.toFixed(0) ?? '—'}%</span>
          </div>
          <div className="flex items-baseline justify-between gap-2 text-xs">
            <span className="text-secondary">收盘位置</span>
            <span className="font-mono">{f.close_position_pct?.toFixed(0) ?? '—'}%</span>
          </div>
          <div className="flex items-baseline justify-between gap-2 text-xs">
            <span className="text-secondary">RSI14</span>
            <span className="font-mono">{f.rsi14?.toFixed(1) ?? '—'}</span>
          </div>
          <div className="flex items-baseline justify-between gap-2 text-xs">
            <span className="text-secondary">ATR14</span>
            <span className="font-mono">{f.atr14?.toFixed(2) ?? '—'} ({f.atr14_pct?.toFixed(2) ?? '—'}%)</span>
          </div>
        </div>
        {/* 把 ATR / 年化波动换算成钱和百分比区间 —— 这是普通投资者真正读得懂的风险口径 */}
        {(dailyYuan != null || monthlyBand != null) && (
          <div className="border-t border-border/40 pt-1.5 space-y-0.5">
            {f.atr14_pct != null && (
              <div className="text-[11px] leading-4 text-secondary">
                · 一天大概晃 <span className="font-mono">±{f.atr14_pct.toFixed(2)}%</span>
                {dailyYuan != null && (
                  <>
                    （100 股约 <span className="font-mono">±{dailyYuan.toFixed(0)} 元</span>；1 万元仓位约{' '}
                    <span className="font-mono">±{((10000 * f.atr14_pct) / 100).toFixed(0)} 元</span>）
                  </>
                )}
              </div>
            )}
            {monthlyBand != null && (
              <div className="text-[11px] leading-4 text-secondary">
                · 按这个波动，一个月大概率在 <span className="font-mono">±{monthlyBand.toFixed(1)}%</span> 内
                （年化波动 {f.vol_20d_annual_pct?.toFixed(1)}% 换算，只是幅度估计，不是涨跌预测）
              </div>
            )}
          </div>
        )}
        <div className="border-t border-border/40 pt-1.5 space-y-0.5">
          {Object.entries(f.mas ?? {}).map(([k, v]) => (
            <PriceLine key={k} label={k} price={v as number} tone={((f.ma_deviation_pct?.[k] ?? 0) < 0 ? 'down' : 'up')} />
          ))}
        </div>
      </div>
    </div>
  )
}

/** 推断段: 判断 + 概率 + 前提, 每条都可被推翻。 */
function InferenceSection({ data }: { data: ActionCardData }) {
  const inf = data.inference
  const st = data.strength
  return (
    <div className="space-y-2">
      <SectionTitle tone="infer" title={data.sections.inference} subtitle="系统的推测，说了依据和历史样本数；以后走势不对就会被推翻，别当成定论" />
      <div className="rounded-btn border border-border/50 px-3 py-2.5 space-y-2">
        <div className="flex items-center gap-2">
          <span className="text-xs text-secondary">方向判定</span>
          <span className={cn('rounded px-2 py-0.5 text-[11px] font-medium',
            st.stance === 'strong' ? 'bg-success/15 text-success'
              : st.stance === 'weak' ? 'bg-danger/15 text-danger' : 'bg-muted/15 text-muted')}>
            {st.label}
          </span>
          <span className="text-[10px] text-muted">看多 {st.bullish_evidence.length} / 看空 {st.bearish_evidence.length}</span>
        </div>
        <ul className="space-y-0.5">
          {st.bullish_evidence.map((e, i) => (
            <li key={`b${i}`} className="text-[11px] leading-4 text-success">+ {e}</li>
          ))}
          {st.bearish_evidence.map((e, i) => (
            <li key={`s${i}`} className="text-[11px] leading-4 text-danger">− {e}</li>
          ))}
          {st.neutral_evidence.map((e, i) => (
            <li key={`n${i}`} className="text-[11px] leading-4 text-muted">· {e}</li>
          ))}
        </ul>
        <div className="border-t border-border/40 pt-1.5 space-y-1">
          <div className="text-[10px] text-muted">{inf.analog_pattern} · 样本 {inf.analog_sample_size} 例 · 置信度 {inf.confidence}</div>
          <ForwardStatsTable stats={inf.forward_stats} />
        </div>
        <div className="text-[10px] leading-4 text-muted">{inf.confidence_basis}</div>
      </div>
    </div>
  )
}

/** 用户计划段: 用户权威, 系统只做对照。 */
function PlanSection({
  data,
  onEdit,
  onDelete,
}: {
  data: ActionCardData
  onEdit: () => void
  onDelete: () => void
}) {
  const p = data.user_plan
  const prog = p.progress
  return (
    <div className="space-y-2">
      <SectionTitle tone="plan" title={data.sections.user_plan} subtitle="这些是你自己填的打算，系统不评价好坏，只把它和真实行情摆在一起给你对照" />
      <div className="rounded-btn border border-border/50 px-3 py-2.5 space-y-1.5">
        {p.counts_toward_pnl ? (
          <div className="text-[11px] text-accent">✓ 实际持仓 · 计入盈亏与权重</div>
        ) : (
          <div className="text-[11px] text-warning">✗ 不计入任何盈亏与权重 · {p.state_note}</div>
        )}
        <div className="flex items-baseline justify-between gap-2 text-xs">
          <span className="text-secondary">计划持有期</span>
          <span className="font-mono">{p.planned_hold_days ? `${p.planned_hold_days} 天` : '未设置'}</span>
        </div>
        <div className="flex items-baseline justify-between gap-2 text-xs">
          <span className="text-secondary">可接受亏损</span>
          <span className="font-mono">{p.acceptable_loss_pct ? `${p.acceptable_loss_pct}%` : '未设置'}</span>
        </div>
        {p.batches.length > 0 && (
          <div className="border-t border-border/40 pt-1.5 space-y-0.5">
            <div className="text-[10px] text-muted">分批计划（{prog?.done_batches ?? 0}/{p.batches.length} 已执行）</div>
            {p.batches.map((b, i) => (
              <div key={i} className="flex items-baseline justify-between gap-2 text-[11px]">
                <span className="text-secondary">{b.label || `第 ${i + 1} 批`}</span>
                <span className="font-mono">
                  {b.price?.toFixed(2)} × {b.amount ? `${b.amount.toLocaleString()} 元` : '—'}
                  {b.done && <span className="ml-1 text-success">已执行</span>}
                </span>
              </div>
            ))}
          </div>
        )}
        {prog && prog.facts.length > 0 && (
          <div className="border-t border-border/40 pt-1.5 space-y-0.5">
            {prog.facts.map((t, i) => <div key={i} className="text-[10px] leading-4 text-secondary">· {t}</div>)}
          </div>
        )}
        {prog && prog.warnings.length > 0 && (
          <div className="space-y-0.5">
            {prog.warnings.map((t, i) => <div key={i} className="text-[10px] leading-4 text-warning">! {t}</div>)}
          </div>
        )}
        <div className="flex gap-2 pt-1">
          <button onClick={onEdit} className="rounded-btn border border-border/60 px-2 py-1 text-[11px] hover:bg-muted/10">
            编辑计划
          </button>
          {p.planned_hold_days || p.batches.length > 0 ? (
            <button onClick={onDelete} className="rounded-btn border border-danger/40 px-2 py-1 text-[11px] text-danger hover:bg-danger/10">
              删除计划
            </button>
          ) : null}
        </div>
      </div>
    </div>
  )
}

/** 四个问题 —— 每个都独立成块, 不合并成总分。 */
function QuestionsSection({ data }: { data: ActionCardData }) {
  return (
    <div className="space-y-3">
      <div>
        <div className="mb-1 text-xs font-medium text-foreground">持有时，什么情况说明原判断可能错了</div>
        {data.invalidation.length === 0 ? (
          <div className="text-[11px] text-muted">没有持仓，无从谈「原判断」；买入后可在此看到具体的失效条件</div>
        ) : (
          <ul className="space-y-1">
            {data.invalidation.map((r, i) => (
              <li key={i} className={cn('rounded-btn border px-2.5 py-1.5 text-[11px] leading-4',
                r.severity === 'critical' ? 'border-danger/30 bg-danger/5' : 'border-warning/30 bg-warning/5')}>
                <div className={r.severity === 'critical' ? 'text-danger' : 'text-warning'}>{r.condition}</div>
                <div className="text-muted">{r.meaning}</div>
                <div className="text-[10px] text-muted/70">依据：{r.source}</div>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <div className="mb-1 text-xs font-medium text-foreground">没买时，哪些条件出现后再考虑</div>
        <ul className="space-y-1">
          {data.entry_conditions.map((c, i) => (
            <li key={i} className="rounded-btn border border-border/50 px-2.5 py-1.5 text-[11px] leading-4">
              <div className="text-secondary">{c.condition}</div>
              <div className="text-muted">{c.rationale}</div>
            </li>
          ))}
        </ul>
      </div>
      <div>
        <div className="mb-1 text-xs font-medium text-foreground">必须核对的事实</div>
        <ul className="space-y-0.5">
          {data.checks.map((c, i) => {
            // 三态必须一眼可分：原来一律渲染 ☐, 把 7 条「已核对」显示成了待办。
            const icon = c.status === 'checked' ? '✓' : c.status === 'unavailable' ? '✗' : '☐'
            const tone = c.status === 'checked' ? 'text-success'
              : c.status === 'unavailable' ? 'text-danger' : 'text-secondary'
            return (
              <li key={i} className="text-[11px] leading-4">
                <span className={tone}>{icon} {c.item}</span>
                <span className="text-muted"> — {c.why}</span>
                <span className="text-[10px] text-muted/70">
                  （{c.where}{c.status === 'unverified' ? ' · 待你核对' : c.status === 'unavailable' ? ' · 本地无数据' : ''}）
                </span>
              </li>
            )
          })}
        </ul>
      </div>
    </div>
  )
}

/** 计划编辑表单。 */
function PlanEditor({
  symbol,
  initial,
  onClose,
  onSaved,
}: {
  symbol: string
  initial?: ActionCardData['user_plan']
  onClose: () => void
  onSaved: () => void
}) {
  const qc = useQueryClient()
  const [holdDays, setHoldDays] = useState(String(initial?.planned_hold_days ?? ''))
  const [lossPct, setLossPct] = useState(String(initial?.acceptable_loss_pct ?? ''))
  const [note, setNote] = useState(initial?.note ?? '')
  const [batches, setBatches] = useState<PositionPlanBatch[]>(
    initial?.batches?.length ? initial.batches : [{ price: 0, amount: 0, label: '', done: false }],
  )

  const save = useMutation({
    mutationFn: () =>
      api.positionPlanSave(symbol, {
        symbol,
        planned_hold_days: holdDays.trim() ? Number(holdDays) : null,
        acceptable_loss_pct: lossPct.trim() ? Number(lossPct) : null,
        batches: batches
          .filter(b => Number(b.price) > 0)
          .map(b => ({ price: Number(b.price), amount: Number(b.amount) || 0, label: b.label ?? '', done: !!b.done })),
        note,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.actionCard(symbol) })
      onSaved()
      onClose()
    },
  })

  const inputCls = 'w-full rounded-btn border border-border/60 bg-transparent px-2 py-1 text-xs font-mono'
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-md rounded-lg border border-border bg-base p-4 space-y-3" onClick={e => e.stopPropagation()}>
        <div className="text-sm font-medium">编辑买入计划 · {symbol}</div>
        <div className="text-[10px] leading-4 text-warning">
          计划只是记录，不会产生任何订单，也不计入盈亏。实际成交请在模拟盘/真实持仓里记录。
        </div>
        <div className="grid grid-cols-2 gap-2">
          <label className="space-y-1">
            <span className="text-[10px] text-secondary">计划持有（天）</span>
            <input value={holdDays} onChange={e => setHoldDays(e.target.value)} className={inputCls} placeholder="30" />
          </label>
          <label className="space-y-1">
            <span className="text-[10px] text-secondary">可接受亏损（%）</span>
            <input value={lossPct} onChange={e => setLossPct(e.target.value)} className={inputCls} placeholder="8" />
          </label>
        </div>
        <div className="space-y-1">
          <div className="text-[10px] text-secondary">分批价格与资金</div>
          {batches.map((b, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <input value={b.label ?? ''} onChange={e => {
                const next = [...batches]; next[i] = { ...next[i], label: e.target.value }; setBatches(next)
              }} className={inputCls} placeholder="说明" />
              <input value={b.price || ''} onChange={e => {
                const next = [...batches]; next[i] = { ...next[i], price: Number(e.target.value) }; setBatches(next)
              }} className={inputCls} placeholder="价格" />
              <input value={b.amount || ''} onChange={e => {
                const next = [...batches]; next[i] = { ...next[i], amount: Number(e.target.value) }; setBatches(next)
              }} className={inputCls} placeholder="金额" />
              <button onClick={() => setBatches(batches.filter((_, j) => j !== i))}
                className="shrink-0 text-muted hover:text-danger"><Trash2 className="h-3.5 w-3.5" /></button>
            </div>
          ))}
          <button onClick={() => setBatches([...batches, { price: 0, amount: 0, label: '', done: false }])}
            className="inline-flex items-center gap-1 text-[11px] text-accent">
            <Plus className="h-3 w-3" />加一批
          </button>
        </div>
        <label className="block space-y-1">
          <span className="text-[10px] text-secondary">备注</span>
          <textarea value={note} onChange={e => setNote(e.target.value)} rows={2}
            className="w-full rounded-btn border border-border/60 bg-transparent px-2 py-1 text-xs" />
        </label>
        {save.error && <div className="text-[11px] text-danger">{String((save.error as Error).message)}</div>}
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onClose} className="rounded-btn border border-border/60 px-3 py-1 text-xs">取消</button>
          <button onClick={() => save.mutate()} disabled={save.isPending}
            className="rounded-btn bg-accent px-3 py-1 text-xs text-white disabled:opacity-50">
            {save.isPending ? '保存中…' : '保存计划'}
          </button>
        </div>
      </div>
    </div>
  )
}

/** 基本面段: 财报与公告的自动解析结果 —— 把"去哪查"变成"查到了什么"。 */
// key 必须与后端 fundamentals.metrics 的真实命名一致（backend 财务指标解析产出这 11 个 key）：
// 盈利能力 roe/roa/gross_margin/net_margin、成长 revenue_yoy/net_income_yoy/total_assets_growth_ratio、
// 杠杆 debt_to_asset_ratio/current_ratio、每股 eps_basic/bps。
// 这里按「先看赚钱能力、再看成长、最后看欠了多少钱」挑四项；一个都对不上时退化为原顺序前 4 项。
const CORE_METRIC_KEYS = ['roe', 'revenue_yoy', 'net_income_yoy', 'debt_to_asset_ratio']

function FundamentalsSection({ data }: { data: ActionCardData }) {
  const f = data.fundamentals
  const [open, setOpen] = useState(false)
  if (!f) return null

  // 无数据时压成一行：9 只持仓里多数没有财报, 原来每只都占一整块, 首屏被「本地无数据」占满。
  if (!f.available && !f.announcements?.length) {
    return (
      <div className="text-[11px] leading-4 text-muted">
        财报与公告：本地无数据（
        {(f.limitations ?? []).join('；') || '该标的没有已落库的财务记录与近期公告'}）
      </div>
    )
  }

  const core = f.metrics.filter(m => CORE_METRIC_KEYS.includes(m.key))
  const picked = core.length >= 3 ? core.slice(0, 4) : f.metrics.slice(0, 4)
  const rest = f.metrics.filter(m => !picked.includes(m))

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-xs font-medium">财报与公告</span>
        {f.latest_period && (
          <span className="text-[10px] text-muted">
            报告期 {f.latest_period}
            {f.latest_announce_date && ` · 公告于 ${f.latest_announce_date}`}
          </span>
        )}
        <span className="ml-auto text-[10px] text-muted">已按公告日过滤，不用未来数据</span>
      </div>
      <div className="rounded-btn border border-border/50 px-3 py-2 space-y-1.5">
        {picked.length > 0 && (
          <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
            {picked.map(m => (
              <div key={m.key} className="flex items-baseline justify-between gap-2 text-[11px]">
                <span className="text-secondary">{m.label}</span>
                <span className={cn('font-mono', (m.value ?? 0) < 0 ? 'text-danger' : 'text-foreground')}>
                  {m.value}{m.unit}
                </span>
              </div>
            ))}
          </div>
        )}
        {rest.length > 0 && (
          <>
            <button type="button" onClick={() => setOpen(v => !v)} aria-expanded={open}
              className="inline-flex items-center gap-1 text-[10px] text-accent hover:underline">
              <ChevronDown className={cn('h-3 w-3 transition-transform', open && 'rotate-180')} />
              {open ? '收起' : `展开其余 ${rest.length} 项财务指标`}
            </button>
            {open && (
              <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 border-t border-border/40 pt-1">
                {rest.map(m => (
                  <div key={m.key} className="flex items-baseline justify-between gap-2 text-[11px]">
                    <span className="text-secondary">{m.label}</span>
                    <span className={cn('font-mono', (m.value ?? 0) < 0 ? 'text-danger' : 'text-foreground')}>
                      {m.value}{m.unit}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
        {(f.revenue_trend?.length ?? 0) > 1 && (
          <div className="border-t border-border/40 pt-1 text-[10px] text-muted">
            营收同比：
            {f.revenue_trend.map((t, i) => (
              <span key={i} className="ml-1.5">{t.period} {t.value}%</span>
            ))}
          </div>
        )}
        {(f.announcements?.length ?? 0) > 0 && (
          <div className="border-t border-border/40 pt-1 space-y-0.5">
            <div className="text-[10px] text-muted">近期公告</div>
            {f.announcements.map((a, i) => (
              <div key={i} className="text-[10px] leading-4">
                <span className="text-secondary">{a.label}</span>
                {a.date && <span className="ml-1 text-muted">({a.date})</span>}
                <span className="ml-1 font-mono text-muted">
                  {Object.entries(a.fields).slice(0, 3).map(([k, v]) => `${k}=${v}`).join(' ')}
                </span>
              </div>
            ))}
          </div>
        )}
        {(f.limitations?.length ?? 0) > 0 && (
          <div className="border-t border-border/40 pt-1 space-y-0.5">
            {f.limitations.map((l, i) => (
              <div key={i} className="text-[10px] leading-4 text-muted">· {l}</div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

/** 规则有效性提示 —— 敢说规则没用，才是诚实的卡。 */
function RuleValidationHint({ data }: { data: ActionCardData }) {
  const rv = data.inference.rule_validation
  if (!rv || !rv.verdict) return null
  const style = {
    validated: 'border-success/30 bg-success/5 text-success',
    weak_signal: 'border-warning/30 bg-warning/5 text-warning',
    inverted: 'border-danger/40 bg-danger/10 text-danger',
    no_edge: 'border-muted/30 bg-muted/5 text-muted',
    insufficient: 'border-muted/30 bg-muted/5 text-muted',
  }[rv.verdict] ?? 'border-muted/30 bg-muted/5 text-muted'
  const label = {
    validated: '有效', weak_signal: '弱有效', inverted: '反向指标',
    no_edge: '无预测力', insufficient: '样本不足',
  }[rv.verdict] ?? rv.verdict
  const basis = rv.basis
  return (
    <div className={cn('rounded-btn border px-3 py-2', style)}>
      <div className="text-[11px] font-medium">
        规则有效性：{label}（本档 n={rv.samples}）
      </div>
      {basis && (
        <div className="text-[10px] leading-4 opacity-70">
          {basis.title} · {basis.study_date} · {basis.symbols_used} 只票 /{' '}
          {basis.sampled_points} 采样点（{basis.date_from}→{basis.date_to}）
        </div>
      )}
      <div className="text-[10px] leading-4 opacity-80">{rv.note}</div>
    </div>
  )
}

/** 计划与实际成交对账。 */
function ReconcileSection({ data }: { data: ActionCardData }) {
  const r = data.reconciliation
  if (!r || !r.matches?.length) return null
  return (
    <div className="space-y-1">
      <div className="text-xs font-medium">计划 vs 实际成交</div>
      <div className="rounded-btn border border-border/50 px-3 py-2 space-y-0.5">
        <div className="text-[10px] text-muted">
          已匹配 {r.matched_count} / 未匹配 {r.unmatched_batches}
          {r.orphan_fills > 0 && ` / 计划外成交 ${r.orphan_fills}`}
        </div>
        {r.matches.map((m, i) => (
          <div key={i} className="flex items-baseline gap-2 text-[10px] leading-4">
            {m.status === 'matched'
              ? <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0 text-success" />
              : <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-muted" />}
            <span className="text-secondary">{m.batch_label}</span>
            <span className="font-mono">
              {m.batch_price.toFixed(2)}
              {m.fill_price != null && ` → ${m.fill_price.toFixed(2)}`}
            </span>
            {m.price_diff_pct != null && (
              <span className={cn('font-mono',
                Math.abs(m.price_diff_pct) > 2 ? 'text-warning' : 'text-muted')}>
                {m.price_diff_pct > 0 ? '+' : ''}{m.price_diff_pct}%
              </span>
            )}
            {m.fill_date && <span className="ml-auto text-muted">{m.fill_date}</span>}
          </div>
        ))}
        {r.warnings.map((w, i) => (
          <div key={i} className="text-[10px] leading-4 text-warning">! {w}</div>
        ))}
        {r.suggested_actions.map((s, i) => (
          <div key={i} className="text-[10px] leading-4 text-muted">→ {s}</div>
        ))}
      </div>
    </div>
  )
}

export function ActionCard() {
  // 支持 /action-card?symbol=xxx 直接落到某只票 —— 持仓总览三态清单的
  // 「行动卡 →」靠它跳转。不读 URL 的话点进来还是空页，看着像没反应。
  const [searchParams] = useSearchParams()
  const urlSymbol = (searchParams.get('symbol') ?? '').trim().toUpperCase()
  const [symbol, setSymbol] = useState(urlSymbol)
  const [active, setActive] = useState(urlSymbol)
  const [editing, setEditing] = useState(false)
  const qc = useQueryClient()

  // 已在行动卡页时再点另一只票的链接: URL 变了但组件没重挂, 需要跟上。
  useEffect(() => {
    if (!urlSymbol) return
    setSymbol(urlSymbol)
    setActive(urlSymbol)
  }, [urlSymbol])

  // 我的持仓快捷入口：录过真实持仓后，行动卡顶部直接列出代码，点一下就能看，
  // 不用每次手敲 —— 否则用户会觉得「录了持仓，行动卡还是空的」。
  const holdings = useQuery({
    queryKey: QK.manualHoldings,
    queryFn: () => api.manualHoldings(),
  })

  const card = useQuery({
    queryKey: QK.actionCard(active),
    queryFn: () => api.actionCard(active),
    enabled: !!active,
  })

  const del = useMutation({
    mutationFn: () => api.positionPlanDelete(active),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.actionCard(active) })
    },
  })

  const data = card.data
  const caveats = useMemo(() => data?.caveats ?? [], [data])

  return (
    <div className="tsp-readable flex h-full flex-col">
      <PageHeader
        title="行动卡"
        subtitle="把「现在的情况 / 系统的推测 / 你的打算」分开摆 · 不打分、不荐股"
      />
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <div className="mx-auto max-w-3xl space-y-4">
          <div className="flex gap-2">
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <input
                value={symbol}
                onChange={e => setSymbol(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && symbol.trim()) setActive(symbol.trim().toUpperCase()) }}
                placeholder="输入代码，如 600941.SH / 605366.SH"
                className="w-full rounded-btn border border-border/60 bg-transparent py-1.5 pl-8 pr-3 text-xs"
              />
            </div>
            <button
              onClick={() => symbol.trim() && setActive(symbol.trim().toUpperCase())}
              className="rounded-btn bg-accent px-3 py-1.5 text-xs text-white"
            >
              查看
            </button>
          </div>

          <PlainGuide
            intro="这张卡只回答三件事：现在到底是什么情况（事实）、系统据此觉得大概会怎样（推断）、你自己原本打算怎么做（计划）。它不打分、不荐股、也不替你下单。卡里的英文缩写和专业名词，点开下面的「名词解释」都有大白话翻译。"
            terms={ACTION_CARD_TERMS}
          />

          {(holdings.data?.positions?.length ?? 0) > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] text-muted">我的持仓：</span>
              {holdings.data!.positions.map(p => (
                <button key={p.symbol}
                  onClick={() => { setActive(p.symbol); setSymbol(p.symbol) }}
                  className={cn('rounded border px-2 py-0.5 font-mono text-[11px]',
                    active === p.symbol
                      ? 'border-accent/50 bg-accent/10 text-accent'
                      : 'border-border/60 text-secondary hover:bg-elevated')}>
                  {p.symbol}{p.name ? ` ${p.name}` : ''}
                </button>
              ))}
            </div>
          )}

          {!active && (
            <div className="rounded-btn border border-border/50 px-4 py-6 text-center text-xs text-muted">
              输入代码或名称后查看行动卡（如 000333.SZ / 美的集团）。卡里有三块内容分开显示：<br />
              <span className="text-foreground">事实</span>（可核对）·
              <span className="text-accent">系统推断</span>（可被推翻）·
              <span className="text-warning">你的计划</span>（你填的）
            </div>
          )}

          {card.isLoading && <Skeleton className="h-40 w-full" />}
          {card.error && (
            <div className="rounded-btn border border-danger/40 bg-danger/5 px-3 py-2 text-xs text-danger">
              {(card.error as Error).message}
            </div>
          )}

          {data && (
            <div className="space-y-4">
              {/* 结论带 + 价格地图在最上层：普通投资者第一眼要的是「什么票、多少钱、我赚多少、
                  上面压什么、下面撑什么」，三段结构保持不动，只在其上叠一个聚合层。 */}
              <SummaryBand data={data} />
              <PriceLadder data={data} />

              {caveats.length > 0 && (
                <div className="rounded-btn border border-muted/30 bg-muted/5 px-3 py-2 space-y-0.5">
                  {caveats.map((c, i) => (
                    <div key={i} className="text-[10px] leading-4 text-muted">· {c}</div>
                  ))}
                </div>
              )}

              <FactsSection data={data} />
              <FundamentalsSection data={data} />
              <InferenceSection data={data} />
              <RuleValidationHint data={data} />
              <PlanSection data={data} onEdit={() => setEditing(true)} onDelete={() => del.mutate()} />
              <ReconcileSection data={data} />
              <div className="rounded-btn border border-border/50 px-3 py-2.5">
                <QuestionsSection data={data} />
              </div>
            </div>
          )}
        </div>
      </div>

      {editing && data && (
        <PlanEditor
          symbol={data.symbol}
          initial={data.user_plan}
          onClose={() => setEditing(false)}
          onSaved={() => void qc.invalidateQueries({ queryKey: QK.actionCard(data.symbol) })}
        />
      )}
    </div>
  )
}
