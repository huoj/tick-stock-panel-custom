import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Loader2, ShieldAlert, Wallet } from 'lucide-react'
import { api, type ManualHoldingPosition, type PortfolioRiskReport } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'

const BUCKET_TEXT: Record<string, string> = {
  high: '充足',
  mid: '中等',
  low: '偏薄',
  unknown: '未知',
}

function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

/** 后端给的是百分数（如 16.5895 表示 16.59%），不要再乘 100。 */
function pctNum(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value.toFixed(digits)}%`
}

/**
 * 把「可能是小数、也可能是百分数」的比例统一成百分数。
 *
 * 同一个报告里两种口径并存，直接用会显示错 100 倍：
 * - ``volatility``      = 0.663   → 66.3%（小数口径，backend `portfolio_risk.py` ×√252 后未 ×100）
 * - ``drawdown_pct``    = 0.3922  → 39.22%（小数口径，PositionsInput 注释「0.05 = 5%」）
 * - ``stressed_drawdown_pct`` / ``budget_usage_pct`` / ``top_industry_share``
 *                       = 13.02 / 86.82 / 0.4903（前两个已是百分数，第三个是小数）
 *
 * 判定规则：绝对值 ≤ 1 当小数 ×100，> 1 当已是百分数原样返回；0 和 1 的歧义按
 * 「小数」处理（0 → 0%、1 → 100%），因为组合比例类字段几乎没有恰好等于 1% 的语义，
 * 而 100% 全仓是真实会出现的取值。负数取绝对值判断后复原符号。
 */
function asPct(value: number | null | undefined): number | null {
  if (value === null || value === undefined || Number.isNaN(value)) return null
  return Math.abs(value) <= 1 ? value * 100 : value
}

/** 归一化后再格式化，避免把 0.663 显示成「0%」或把 0.39 显示成「0.39%」。 */
function pctAny(value: number | null | undefined, digits = 1): string {
  const v = asPct(value)
  return v === null ? '—' : `${v.toFixed(digits)}%`
}

function money(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const abs = Math.abs(value)
  // 带上「元」：普通投资者看到「17.16 万」不一定反应过来是钱，补单位才不会误读成百分比或股数。
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)} 亿元`
  if (abs >= 1e4) return `${(value / 1e4).toFixed(2)} 万元`
  return `${value.toFixed(0)} 元`
}

/**
 * 给后端拼好的整句文案补上股票名称。
 *
 * warnings / limitations / note 都是后端已经拼好的一句话（例如
 * 「600001.SH 与 600002.SH 相关系数 0.73」），前端拿不到结构化字段，只能按
 * 代码字面替换；面板自己批量查到的代码→名称映射传进来，表格、相关对、警告
 * 才能一起变成「名称(代码)」。长代码先替换，避免某个代码是另一个代码前缀时误伤。
 * 名称没查到时映射为空，整句原样返回，不影响任何既有口径与断言。
 */
function withNames(text: string, names: Record<string, string>): string {
  const pairs = Object.entries(names)
    .filter(([, name]) => !!name)
    .sort((a, b) => b[0].length - a[0].length)
  let out = text
  for (const [symbol, name] of pairs) {
    out = out.split(symbol).join(`${name}(${symbol})`)
  }
  return out
}

/** 字符串场景（表头清单、相关对、警告）：有名称就写成「名称(代码)」，否则只留代码。 */
function symLabel(symbol: string, names: Record<string, string>): string {
  const name = names[symbol]
  return name ? `${name}(${symbol})` : symbol
}

/** 表格单元格：名称用正文字体突出，代码缩小成灰色 mono 留作核对。 */
function Sym({ symbol, names }: { symbol: string; names: Record<string, string> }) {
  const name = names[symbol]
  if (!name) return <span className="font-mono text-foreground">{symbol}</span>
  return (
    <span>
      <span className="font-sans text-foreground">{name}</span>
      <span className="ml-1 font-mono text-[10px] text-muted">({symbol})</span>
    </span>
  )
}

/**
 * 批量查代码→名称。模拟盘页 / 组合风控页都复用这个共享面板，在这里就地查名称，
 * 比让每个页面各自传一份映射更省心；没查到时 data 为 undefined → names={}，
 * 全页自动退回只显示代码，不影响既有调用方。
 */
function useInstrumentNames(symbols: readonly string[], enabled = true) {
  return useQuery({
    queryKey: QK.instrumentNames(symbols),
    queryFn: () => api.instrumentNames([...symbols]),
    enabled: enabled && symbols.length > 0,
    staleTime: 300_000,
  })
}

/** 每个指标块下的「一句话说明」：数字旁边就讲清它是什么意思、该怎么读。 */
function Hint({ children }: { children: React.ReactNode }) {
  return <p className="mb-1 text-[11px] leading-relaxed text-muted">{children}</p>
}

// ── P0 首屏结论带 ────────────────────────────────────────────────────────

type LightTone = 'ok' | 'warn' | 'bad' | 'unknown'

interface RiskLight {
  key: string
  label: string
  tone: LightTone
  /** 一眼要看到的那个数（如「49%」「87%」） */
  value: string
  /** 这个数是什么意思（如「白色家电」） */
  note: string
  /** 点这个灯跳到哪一块详情 */
  anchor: string
}

const TONE_TEXT: Record<LightTone, string> = {
  ok: 'text-success',
  warn: 'text-warning',
  bad: 'text-danger',
  unknown: 'text-muted',
}

const TONE_DOT: Record<LightTone, string> = {
  ok: 'bg-success',
  warn: 'bg-warning',
  bad: 'bg-danger',
  unknown: 'bg-muted',
}

const TONE_WORD: Record<LightTone, string> = {
  ok: '正常',
  warn: '接近上限',
  bad: '需处理',
  unknown: '待补数据',
}

/**
 * 把分散在四个子结构里的「该报警的地方」压成四个灯。
 *
 * 普通投资者打开这页想问的是「我整体危不危险」, 而答案原本埋在行业集中度、
 * 回撤预算、单笔风险表、数据质量四个区块里, 要滚完八屏才拼得出来。这里只做
 * **聚合与判定**, 不新增任何后端字段、不改任何既有口径。
 */
function buildLights(report: PortfolioRiskReport): RiskLight[] {
  const lights: RiskLight[] = []
  const ind = report.industry_concentration
  const budget = report.drawdown_budget
  const dq = report.data_quality
  const volatility = report.volatility ?? {}

  // 1) 行业集中：35% 是后端写在 warning 里的警戒线
  if (ind) {
    const share = asPct(ind.top_industry_share) ?? 0
    const tone: LightTone = share >= 35 ? 'bad' : share >= 25 ? 'warn' : 'ok'
    const unclassified = asPct(ind.unclassified_weight) ?? 0
    lights.push({
      key: 'industry',
      label: '行业集中',
      tone,
      value: `${share.toFixed(0)}%`,
      note: ind.top_industry
        ? `${ind.top_industry}${unclassified >= 20 ? ` · ${unclassified.toFixed(1)}% 无行业归属` : ''}`
        : '无行业归属数据',
      anchor: '#risk-industry',
    })
  }

  // 2) 回撤预算：用量是后端已算好的百分数（86.82 表示 86.82%）
  if (budget) {
    const usage = budget.budget_usage_pct
    const tone: LightTone = budget.over_budget
      ? 'bad'
      : usage != null && usage >= 80 ? 'warn' : 'ok'
    lights.push({
      key: 'budget',
      label: '回撤预算',
      tone,
      value: usage == null ? '—' : `${usage.toFixed(0)}%`,
      note: `压力回撤 ${budget.stressed_drawdown_pct.toFixed(1)}% / 预算 ${budget.budget_pct}%`,
      anchor: '#risk-budget',
    })
  }

  // 3) 单票波动：volatility 是小口径（0.663 = 66.3%），必须过 asPct 再比
  const volEntries = Object.entries(volatility)
    .map(([symbol, v]) => [symbol, asPct(v)] as const)
    .filter((e): e is readonly [string, number] => e[1] !== null)
    .sort((a, b) => b[1] - a[1])
  if (volEntries.length > 0) {
    const [topSymbol, topVol] = volEntries[0]
    const tone: LightTone = topVol >= 60 ? 'bad' : topVol >= 40 ? 'warn' : 'ok'
    lights.push({
      key: 'volatility',
      label: '单票波动',
      tone,
      value: `${topVol.toFixed(0)}%`,
      note: `最高一只 · ${symLabel(topSymbol, {})}`,
      anchor: '#risk-holdings',
    })
  }

  // 4) 数据质量：门禁不过或数据陈旧时，上面所有百分比都只能当参考
  if (dq) {
    const tone: LightTone = dq.stale || dq.verdict === 'reference'
      ? 'warn'
      : dq.verdict === 'ok' ? 'ok' : 'unknown'
    lights.push({
      key: 'data',
      label: '数据质量',
      tone,
      value: dq.data_through ?? dq.as_of ?? '—',
      note: dq.common_days != null ? `共同 ${dq.common_days} 个交易日` : '详见口径说明',
      anchor: '#risk-advanced',
    })
  }

  return lights
}

/**
 * 把专业指标翻译成人话——普通投资者「看懂数字」不代表「会正确解读」。
 * 重点防两种入门者最常犯的误判：
 *   ① 把「低 beta 跌得少」当成选股能力强 -> 行情一转就打回原形；
 *   ② 只看「行业 49%」这个数，没意识到等于半仓押一个行业会共振下跌。
 * 只读既有 report 字段，不新增后端口径。
 */
function buildPlainTalk(report: PortfolioRiskReport): string[] {
  const out: string[] = []
  const ind = report.industry_concentration
  const budget = report.drawdown_budget
  const rel = report.relative_performance

  if (ind) {
    const share = asPct(ind.top_industry_share) ?? 0
    if (share >= 25 && ind.top_industry) {
      out.push(`你最大的行业「${ind.top_industry}」占了 ${share.toFixed(0)}%，等于把近一半的钱押在同一个行业，它一跌你手里的票会一起跌。`)
    }
  }

  const heavy = (report.positions ?? []).filter(p => (p.weight ?? 0) >= 0.2)
  if (heavy.length >= 2) {
    const sum = heavy.reduce((s, p) => s + (p.weight ?? 0), 0)
    out.push(`你有 ${heavy.length} 只票各占 20% 以上、合计约 ${(sum * 100).toFixed(0)}%，钱过度集中在少数几只上，不算分散投资。`)
  }

  if (rel && typeof rel.excess_return === 'number' && typeof rel.beta === 'number'
      && rel.excess_return > 0 && rel.beta < 0.6) {
    out.push(`你这段时间「跑赢大盘 ${(rel.excess_return * 100).toFixed(1)}%」，主要是因为波动低（beta ${rel.beta.toFixed(2)}）跌得少，不是选股更准；行情一旦大涨，你可能反而跑输。`)
  }

  if (budget && typeof budget.budget_usage_pct === 'number' && budget.budget_usage_pct >= 70) {
    out.push(`你的亏损已经用掉 ${budget.budget_usage_pct.toFixed(0)}% 的回撤容忍额度，再往下就逼近该减仓的位置了。`)
  }

  return out
}

/** 首屏一句话结论：先给判断，再给四盏灯，避免「打开只看到表格」。 */
function ConclusionBand({
  report, lights, cashNotEntered,
}: {
  report: PortfolioRiskReport
  lights: RiskLight[]
  cashNotEntered: boolean
}) {
  const bad = lights.filter(l => l.tone === 'bad')
  const warn = lights.filter(l => l.tone === 'warn')
  const plainTalk = buildPlainTalk(report)
  const headline = bad.length > 0
    ? `有 ${bad.length} 项需要处理`
    : warn.length > 0
      ? `暂未触线，但有 ${warn.length} 项已接近上限`
      : '组合结构暂时还算稳'
  const detail = [...bad, ...warn].map(l => `${l.label} ${l.value}`).join(' · ')

  return (
    <section
      className="rounded-btn border border-border bg-elevated/40 px-3 py-2.5"
      aria-label="组合风险结论"
    >
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
        <span className="text-sm font-medium text-foreground">{headline}</span>
        {report.account_equity != null && (
          <span className="text-xs text-secondary">
            账户权益 <span className="font-mono text-foreground">{money(report.account_equity)}</span>
          </span>
        )}
        {report.total_pnl != null && (
          <span className={cn('text-xs font-medium', report.total_pnl >= 0 ? 'text-success' : 'text-danger')}>
            总盈亏 <span className="font-mono">{money(report.total_pnl)}</span>
          </span>
        )}
        <span className="ml-auto text-[11px] text-muted">
          真实持仓 {report.positions_fed ?? report.positions.length} 只
        </span>
      </div>
      {detail && <p className="mt-0.5 text-xs text-secondary">{detail}</p>}
      {cashNotEntered && (
        <p className="mt-0.5 text-[11px] text-warning">
          现金还没录入，仓位比例是按「满仓」估算的，不是实际满仓。
        </p>
      )}
      {plainTalk.length > 0 && (
        <div className="mt-2 rounded-btn border border-warning/30 bg-warning/5 px-2.5 py-2">
          <div className="mb-1 flex items-center gap-1 text-[11px] font-medium text-warning">
            <span aria-hidden>💡</span> 用大白话说，你现在要留意的：
          </div>
          <ul className="space-y-1">
            {plainTalk.map((line, i) => (
              <li key={i} className="text-[11px] leading-relaxed text-secondary">· {line}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-2 grid grid-cols-2 gap-1.5 sm:grid-cols-4">
        {lights.map(l => (
          <a
            key={l.key}
            href={l.anchor}
            className="group rounded-btn border border-border/60 bg-surface px-2 py-1.5 hover:border-accent/40"
            title={`${l.label}：${l.value}（${TONE_WORD[l.tone]}）—— 点开看详情`}
          >
            <div className="flex items-center gap-1 text-[11px] text-muted">
              <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', TONE_DOT[l.tone])} />
              {l.label}
            </div>
            <div className={cn('mt-0.5 font-mono text-base font-semibold tabular-nums', TONE_TEXT[l.tone])}>
              {l.value}
            </div>
            <div className="truncate text-[10px] text-muted" title={l.note}>{l.note}</div>
          </a>
        ))}
      </div>
    </section>
  )
}

// ── P1 持仓速览（把「已请求到却没渲染」的持仓明细一次摊开） ──────────────

interface QuickRow {
  symbol: string
  name?: string
  lastPrice: number | null
  marketValue: number | null
  weight: number | null
  pnl: number | null
  pnlPct: number | null
  holdingDays: number | null
  industry: string | null
  vol: number | null
  dd: number | null
  rc: number | null
  negativeCost: boolean
}

/**
 * 按 symbol 把三处数据 join 成一行：报告里的 weight/回撤/风险贡献 +
 * 顶层 ``volatility`` + 页面已经拿到的手工持仓明细（名称/现价/市值/盈亏/持有天数）。
 *
 * 关键点：名称、现价、市值、盈亏、成本、持有天数**本来就在** ``/api/manual-holdings``
 * 的 ``positions`` 里，页面早已请求过，此前却只用了 ``count``。这里只是把它渲染出来。
 */
function buildQuickRows(
  report: PortfolioRiskReport,
  manualPositions: readonly ManualHoldingPosition[] | undefined,
): QuickRow[] {
  const map = new Map<string, QuickRow>()
  const ensure = (symbol: string): QuickRow => {
    const found = map.get(symbol)
    if (found) return found
    const created: QuickRow = {
      symbol, lastPrice: null, marketValue: null, weight: null, pnl: null,
      pnlPct: null, holdingDays: null, industry: null, vol: null, dd: null,
      rc: null, negativeCost: false,
    }
    map.set(symbol, created)
    return created
  }

  for (const p of manualPositions ?? []) {
    const row = ensure(p.symbol)
    row.name = p.name
    row.lastPrice = p.last_price ?? null
    row.marketValue = p.market_value ?? null
    row.weight = p.weight ?? null
    row.pnl = p.pnl ?? null
    row.pnlPct = p.pnl_pct ?? null
    row.holdingDays = p.holding_days ?? null
    row.negativeCost = (p.avg_cost ?? 0) < 0
  }

  for (const p of report.positions ?? []) {
    const row = ensure(p.symbol)
    if (row.weight == null) row.weight = p.weight
    row.industry = p.industry
    row.dd = p.drawdown_pct
  }

  for (const p of report.per_position_risk?.positions ?? []) {
    const row = ensure(p.symbol)
    if (row.industry == null) row.industry = p.industry
    if (row.dd == null) row.dd = p.drawdown_pct
    row.rc = p.risk_contribution
  }

  for (const [symbol, v] of Object.entries(report.volatility ?? {})) {
    ensure(symbol).vol = asPct(v)
  }

  // 按风险贡献从大到小：最该先看的排最前；没有风险贡献时退回按权重。
  return [...map.values()].sort(
    (a, b) => (b.rc ?? -1) - (a.rc ?? -1) || (b.weight ?? 0) - (a.weight ?? 0),
  )
}

function HoldingsTable({
  report, names, manualPositions,
}: {
  report: PortfolioRiskReport
  names: Record<string, string>
  manualPositions: readonly ManualHoldingPosition[] | undefined
}) {
  const rows = buildQuickRows(report, manualPositions)
  const risk = report.per_position_risk
  const negCostCount = rows.filter(r => r.negativeCost).length
  const hasPnl = rows.some(r => r.pnl != null)

  return (
    <Block id="risk-holdings" title="持仓速览（单笔风险与盈亏）">
      <Hint>
        一只票一行，按「风险贡献」从大到小排 —— 排最前的就是最该留意的。
        波动、回撤已经换算成百分比；「—」表示本地没有这项数据，不是 0。
      </Hint>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[42rem] text-left text-[11px]">
          <thead className="text-muted">
            <tr>
              <th className="py-1 font-normal">标的</th>
              <th className="py-1 font-normal">行业</th>
              <th className="py-1 text-right font-normal">市值 / 权重</th>
              {hasPnl && <th className="py-1 text-right font-normal">盈亏</th>}
              <th className="py-1 text-right font-normal">年化波动</th>
              <th className="py-1 text-right font-normal">潜在回撤</th>
              <th className="py-1 text-right font-normal">风险贡献</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(row => {
              const heavy = (row.weight ?? 0) >= 0.2
              const wild = (row.vol ?? 0) >= 60
              return (
                <tr key={row.symbol} className="border-t border-border/50">
                  <td className="py-1">
                    <Sym symbol={row.symbol} names={row.name ? { ...names, [row.symbol]: row.name } : names} />
                    <div className="text-[10px] text-muted">
                      {row.lastPrice != null ? `现价 ${row.lastPrice.toFixed(2)}` : '现价 —'}
                      {row.holdingDays != null ? ` · 已持 ${row.holdingDays} 天` : ''}
                    </div>
                  </td>
                  <td className="py-1 text-secondary">{row.industry ?? '—'}</td>
                  <td className="py-1 text-right font-mono">
                    <div>{row.marketValue != null ? money(row.marketValue) : '—'}</div>
                    <div className={cn('font-medium', heavy ? 'text-danger' : 'text-muted')}>
                      {row.weight != null ? pct(row.weight) : '—'}
                      {heavy && <div className="text-[10px] font-normal">超过两成红线</div>}
                    </div>
                  </td>
                  {hasPnl && (
                    <td className={cn(
                      'py-1 text-right font-mono',
                      row.pnl == null ? 'text-muted' : row.pnl >= 0 ? 'text-success' : 'text-danger',
                    )}>
                      <div>{row.pnl != null ? money(row.pnl) : '—'}</div>
                      <div className="text-muted">
                        {row.pnlPct != null ? `${row.pnlPct.toFixed(2)}%` : row.negativeCost ? '成本为负' : '—'}
                      </div>
                    </td>
                  )}
                  <td className={cn('py-1 text-right font-mono', wild ? 'text-danger' : '')}>
                    {row.vol == null ? '—' : `${row.vol.toFixed(0)}%`}
                    {wild && <div className="text-[10px]">波动偏高</div>}
                  </td>
                  <td className="py-1 text-right font-mono">{pctAny(row.dd, 1)}</td>
                  <td className="py-1 text-right font-mono text-foreground">
                    {row.rc == null ? '—' : pct(row.rc, 2)}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-1 text-[11px] text-secondary">
        合计风险贡献 <span className="font-mono text-foreground">{pct(risk.total_risk_contribution, 2)}</span>
      </p>
      {negCostCount > 0 && (
        <p className="mt-1 text-[11px] leading-relaxed text-muted">
          有 {negCostCount} 笔成本为负（反复做 T 把成本摊薄后的正常结果）：收益率百分比不适用，
          只显示盈亏金额，别把它读成「没算出来」。
        </p>
      )}
      {!hasPnl && (
        <p className="mt-1 text-[11px] leading-relaxed text-muted">
          这一栏没有名称、现价与盈亏 —— 当前口径只拿到权重与风险，持仓明细在「我的真实持仓」里。
        </p>
      )}
      {risk.warnings.map((w, i) => <Warn key={i} text={withNames(w, names)} />)}
    </Block>
  )
}

/**
 * 组合层风险面板。
 *
 * 覆盖用户要求的五件事：
 * 1. 单只股票占账户比例 + 跌 10%/20% 对账户的影响（``scenario``）
 * 2. 行业 / 主题重复暴露（``industry_concentration`` + ``concept``）
 * 3. 个股波动、组合最大回撤、现金比例（``volatility`` / ``max_drawdown`` / ``cash_ratio``）
 * 4. 买入前的仓位模拟 —— 见 ``BuySimulationCard``
 *
 * **诚实边界**：没有 ``equity`` 时只给百分点，不给"亏多少钱"。
 * 观察口径（自选股）会明确标注"这不是持仓组合"。
 */
export function PortfolioRiskPanel({
  holdings,
  budgetPct = 15,
  account,
  manual = false,
  equity,
  cash,
  manualPositions,
  autoRun = false,
  warnObservation = false,
}: {
  /** 传了 ``account`` 时忽略 holdings，直接读真实账户 */
  holdings?: { symbol: string; weight: number; drawdown_pct?: number | null }[]
  budgetPct?: number
  account?: string | null
  /** true 时用「我的真实持仓」（手工录入）口径：忽略 holdings/account，直接读手工持仓 */
  manual?: boolean
  equity?: number | null
  cash?: number | null
  /**
   * 手工持仓的估值明细（来自 `GET /api/manual-holdings` 的 `positions`）。
   *
   * 报告本身只有 ``symbol/weight/drawdown_pct/industry`` 四个字段，名称、现价、
   * 市值、盈亏、成本、持有天数都在这个明细里。组合风控页**早就请求过**这份数据
   * （只为判断 ``count > 0``），这里把它接进来，持仓速览才有「这只票是什么、赚
   * 还是亏」。不传则退化成只有权重与风险，不影响模拟盘 / 观察口径。
   */
  manualPositions?: readonly ManualHoldingPosition[]
  /** true 时进页面就算（账户口径默认开，避免多点一次） */
  autoRun?: boolean
  /** 观察口径（自选股当权重）—— 必须在界面上说明这不是组合 */
  warnObservation?: boolean
}) {
  const [enabled, setEnabled] = useState(autoRun)

  const list = holdings ?? []
  // account 优先（模拟盘页既有挂载点），manual 只在没有 account 时生效；
  // 两个口径同时给属于误用，这里保住既有行为而不是静默切到手工真仓。
  const useAccount = Boolean(account)
  const useManual = manual && !account
  const canRun = useAccount || useManual || list.length > 0

  const query = useQuery({
    queryKey: [
      ...QK.customSignals, 'portfolio-risk', account ?? '', useManual ? 'manual' : '',
      list, budgetPct, equity ?? null, cash ?? null,
    ],
    queryFn: () =>
      useAccount
        ? api.portfolioRiskByAccount(account!, { budgetPct, lookbackDays: 60 })
        : useManual
          ? api.portfolioRiskByManual({ budgetPct, lookbackDays: 60 })
          : api.portfolioRisk({
              holdings: list,
              budget_pct: budgetPct,
              lookback_days: 60,
              equity: equity ?? undefined,
              cash: cash ?? undefined,
            }),
    enabled: enabled && canRun,
    staleTime: 60_000,
  })

  const report = query.data
  // 名称查询要覆盖报告里出现过的所有代码：持仓清单（holdings 口径）、报告自带
  // positions、最高相关对、削减建议。manual/account 口径下 list 为空，全靠报告里的
  // positions 才有代码可查。集合去重后排序进 queryKey，避免顺序抖动重复请求。
  const symbols = useMemo(() => {
    const set = new Set<string>(list.map(h => h.symbol))
    for (const p of report?.positions ?? []) set.add(p.symbol)
    for (const pair of [report?.correlation?.highest_pair, report?.correlation?.lowest_pair]) {
      if (pair) { set.add(pair.a); set.add(pair.b) }
    }
    for (const r of report?.per_position_risk?.positions ?? []) set.add(r.symbol)
    for (const r of report?.scenario?.positions ?? []) set.add(r.symbol)
    for (const c of report?.drawdown_budget?.suggested_cuts ?? []) set.add(c.symbol)
    for (const s of Object.keys(report?.volatility ?? {})) set.add(s)
    for (const g of report?.concept?.groups ?? []) for (const s of g.symbols ?? []) set.add(s)
    if (report?.benchmark?.index) set.add(report.benchmark.index)
    return [...set].sort()
  }, [list, report])
  const names = useInstrumentNames(symbols).data?.names ?? {}

  if (!canRun) {
    return <p className="text-xs text-muted">当前没有持仓，无法做组合风险评估。</p>
  }

  return (
    <section className="space-y-3" aria-label="组合风险">
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <span className="text-secondary">
          {useAccount ? '真实持仓（模拟盘账户）'
            : useManual ? '真实持仓（手工录入）'
              : `${list.length} 笔${warnObservation ? '观察标的' : '持仓'}`} · 回撤预算 {budgetPct}%
        </span>
        {!enabled && (
          <button
            onClick={() => setEnabled(true)}
            className="rounded-btn border border-accent/40 bg-accent/10 px-2.5 py-1 text-accent cursor-pointer"
          >
            评估组合风险
          </button>
        )}
        {!useAccount && (
          <span className="text-muted" title={symbols.map(s => symLabel(s, names)).join('、')}>
            {symbols.slice(0, 6).map(s => symLabel(s, names)).join('、')}
            {symbols.length > 6 ? ' …' : ''}
          </span>
        )}
      </div>

      {(warnObservation || (report?.warning && report.basis !== 'actual_positions')) && (
        <p className="flex items-start gap-1.5 rounded-btn border border-warning/30 bg-warning/[0.06] px-2.5 py-1.5 text-[11px] leading-relaxed text-warning">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          {report?.warning
            ? withNames(report.warning, names)
            : '这里把自选清单等权当成组合，它不是你的实际持仓。集中度、回撤预算只有落在真实持仓与资金上才有意义。'}
        </p>
      )}

      {enabled && query.isPending && (
        <p className="flex items-center gap-2 text-xs text-secondary">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />正在计算集中度、相关性与情景冲击…
        </p>
      )}
      {enabled && query.error && (
        <p role="alert" className="text-xs text-danger">{(query.error as Error).message}</p>
      )}
      {report && <ReportBody report={report} names={names} manualPositions={manualPositions} />}
    </section>
  )
}

function ReportBody({
  report, names, manualPositions,
}: {
  report: PortfolioRiskReport
  names: Record<string, string>
  manualPositions?: readonly ManualHoldingPosition[]
}) {
  const ind = report.industry_concentration
  const concept = report.concept
  const corr = report.correlation
  const budget = report.drawdown_budget
  const scenario = report.scenario
  // 手工录入口径同样带账户概览（现金 + 持仓市值 = 权益），只是没有净值序列；
  // 后端对它的 nav 层面字段给 None 并附 nav_limitation，界面照实显示「—」。
  const hasAccount = report.basis === 'actual_positions' || report.basis === 'manual_positions'
  // 没录现金时 invested_ratio 恒为 1，看起来像满仓 —— 这是口径事实，必须写在结论带上，
  // 不能只藏在 limitations 折叠框里（普通投资者不会去点开）。
  const cashNotEntered = (report.account_cash == null || report.account_cash === 0)
    && report.basis === 'manual_positions'
  const lights = buildLights(report)

  return (
    <div className="space-y-3">
      {/* P0 首屏结论带：四盏灯 + 一句话判断，不滚动就能知道「整体危不危险」 */}
      <ConclusionBand report={report} lights={lights} cashNotEntered={cashNotEntered} />

      {/* P1 持仓速览：名称/现价/市值/权重/盈亏/波动/回撤/风险贡献，一行一只 */}
      <HoldingsTable report={report} names={names} manualPositions={manualPositions} />

      {/* 账户概览：现金比例 / 已投资 / 最大回撤 / 基准 */}
      {hasAccount && (
        <Block title="账户概览" icon={Wallet}>
          <Hint>先把账户「总共有多少钱、多少变成了股票、还剩多少现金」摆清楚；后面所有百分比和金额都是以这个账户为分母。回撤是历史上真从最高点跌下来过多少，不是预测。</Hint>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            <Metric label="账户权益" value={money(report.account_equity)} />
            <Metric label="持仓市值" value={money(report.account_market_value)} />
            <Metric
              label="现金"
              value={cashNotEntered ? '未录入' : money(report.account_cash)}
              extra={cashNotEntered
                ? '按满仓口径估算，非实际满仓'
                : report.cash_ratio !== null && report.cash_ratio !== undefined ? `占 ${pct(report.cash_ratio)}` : undefined}
              warn={cashNotEntered || (report.cash_ratio ?? 1) < 0.05}
            />
            <Metric
              label="已投资"
              value={pct(report.invested_ratio)}
              extra={cashNotEntered ? '（未录现金时的推导值）' : undefined}
            />
            <Metric
              label="账户最大回撤"
              value={pctNum(report.max_drawdown)}
              extra="历史实现值"
            />
            <Metric
              label="当前距峰值"
              value={pctNum(report.current_drawdown)}
            />
          </div>
          {report.benchmark && (
            <Metric
              label="对比基准"
              value={`${symLabel(report.benchmark.index, names)} ${pctNum(report.benchmark.benchmark_return_pct)}`}
              extra={`账户 ${pctNum(report.benchmark.account_return_pct)} · 超额 ${pctNum(report.benchmark.excess_pct)}（${report.benchmark.days} 个交易日）`}
            />
          )}
          {report.nav_limitation && <p className="mt-1 text-[11px] text-muted">{withNames(report.nav_limitation, names)}</p>}
          {cashNotEntered && (
            <Warn text="没有录入现金余额，「已投资 100%」只是推导结果 —— 若你还有现金或其它资产没录，实际仓位比这里显示的低。" />
          )}
          {!cashNotEntered && (report.cash_ratio ?? 1) < 0.05 && (
            <Warn text={`现金只剩 ${pct(report.cash_ratio)}，几乎没有缓冲——一次深回撤后没有加仓能力，也无法应对取现。`} />
          )}
        </Block>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        {/* 行业集中度 */}
        <Block id="risk-industry" title="行业集中度">
          <Hint>钱是不是都压在同一个行业里。同一个行业的股票常常一起跌，行业越集中，分散效果越差；「有效行业数」就是把这份集中程度换算成「相当于分散在几个行业」。</Hint>
          <Metric label="最大行业" value={ind.top_industry ?? '—'} extra={pct(ind.top_industry_share)} />
          <Metric label="HHI（集中度）" value={ind.hhi.toFixed(3)} extra={`有效行业数 ${ind.effective_industries}`} />
          {ind.unclassified_weight > 0 && (
            <Metric label="无行业归属" value={pct(ind.unclassified_weight)} warn />
          )}
          <ul className="mt-1 space-y-0.5 text-[11px] text-secondary">
            {Object.entries(ind.by_industry).slice(0, 5).map(([name, share]) => (
              <li key={name} className="flex justify-between gap-2">
                <span className="truncate">{name}</span>
                <span className="shrink-0 font-mono text-foreground">{pct(share)}</span>
              </li>
            ))}
          </ul>
          {ind.warnings.map((w, i) => <Warn key={i} text={withNames(w, names)} />)}
        </Block>

        {/* 相关性 */}
        <Block id="risk-correlation" title="相关性">
          <Hint>看这些票是不是总一起涨跌。「平均相关」越高、走势最像的一对越接近，就越像把钱反复押在同一件事上；「有效独立注数」是把这种同涨同跌折算后，你实际等于下了几笔互不重复的注。</Hint>
          <Metric
            label="平均相关"
            value={corr.average_correlation === null ? '—' : corr.average_correlation.toFixed(2)}
            extra={corr.common_days ? `共同 ${corr.common_days} 个交易日` : undefined}
          />
          <Metric
            label="有效独立注数"
            value={corr.effective_bets === null ? '无定义' : corr.effective_bets.toFixed(1)}
            extra={corr.effective_bets === null ? '权重组合近似完全对冲' : '名义笔数之外的真相'}
          />
          {corr.highest_pair && (
            <>
              <Metric
                label="走势最像的一对"
                value={`${corr.highest_pair.a} ↔ ${corr.highest_pair.b}`}
                extra={`相关系数 ${corr.highest_pair.correlation.toFixed(2)}（越接近 1 越同步）`}
              />
              {(names[corr.highest_pair.a] || names[corr.highest_pair.b]) && (
                <p className="mt-0.5 text-[11px] text-muted">
                  也就是 {symLabel(corr.highest_pair.a, names)} 和 {symLabel(corr.highest_pair.b, names)}，涨跌节奏最接近，接近同一笔风险。
                </p>
              )}
            </>
          )}
          {corr.warnings.map((w, i) => <Warn key={i} text={withNames(w, names)} />)}
        </Block>
      </div>

      {/* 主题重复暴露 */}
      {concept && (
        <Block title="主题重复（概念）暴露">
          {concept.groups.length === 0 ? (
            <p className="text-[11px] text-secondary">没有被 2 只以上持仓共享的概念。</p>
          ) : (
            <>
              <p className="mb-1 text-[11px] leading-relaxed text-muted">
                同一概念被多只持仓共享时，它们在同一个风险驱动下一起动 —— 名义上 10 只票，
                实际可能是 3 个赌注。
              </p>
              <ul className="space-y-0.5 text-[11px]">
                {concept.groups.slice(0, 8).map(g => (
                  <li key={g.concept} className="flex items-baseline justify-between gap-2">
                    <span className="min-w-0 truncate text-secondary">
                      {g.concept}
                      <span className="ml-1 text-muted">×{g.symbol_count}</span>
                      {g.members && g.members.length > 0 && (
                        <span className="ml-1 text-[10px] text-muted/70">
                          （含 {g.members.join('、')}）
                        </span>
                      )}
                    </span>
                    <span className="shrink-0 font-mono text-foreground">{pct(g.weight)}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {(concept.clusters?.length ?? 0) > 0 && (
            <details className="mt-1">
              <summary className="cursor-pointer text-[10px] text-muted">
                近义标签已归并 {concept.clusters!.length} 组（按标的包含度，不是语义理解）
              </summary>
              <div className="mt-0.5 space-y-0.5 text-[10px] text-muted">
                {concept.clusters!.slice(0, 10).map(cl => (
                  <div key={cl.label}>
                    <span className="text-secondary">{cl.label}</span>
                    <span className="ml-1">← {cl.members.filter(m => m !== cl.label).join('、') || '（单标签）'}</span>
                  </div>
                ))}
              </div>
            </details>
          )}
          {(concept.overlaps?.length ?? 0) > 0 && (
            <details className="mt-1">
              <summary className="cursor-pointer text-[10px] text-muted">
                {concept.overlaps!.length} 个标签横跨多个概念，未强行归并
              </summary>
              <div className="mt-0.5 space-y-0.5 text-[10px] text-muted">
                {concept.overlaps!.slice(0, 8).map(ov => (
                  <div key={ov.tag}>
                    <span className="text-secondary">{ov.tag}</span>
                    <span className="ml-1">
                      {ov.candidates
                        .map(c => `${c.parent} ${c.containment_pct.toFixed(0)}%`)
                        .join(' / ')}
                      —— 差别太小，并到任一边都会误导
                    </span>
                  </div>
                ))}
              </div>
            </details>
          )}
          {concept.excluded_generic.length > 0 && (
            <details className="mt-1">
              <summary className="cursor-pointer text-[10px] text-muted">
                已剔除 {concept.excluded_generic.length} 个通用标签（覆盖率过高，任意两只票都会"共享"）
              </summary>
              <div className="mt-0.5 flex flex-wrap gap-1 text-[10px] text-muted">
                {concept.excluded_generic.slice(0, 10).map(g => (
                  <span key={g.concept} className="rounded bg-elevated/50 px-1">
                    {g.concept} {pct(g.coverage_pct, 0)}
                  </span>
                ))}
              </div>
            </details>
          )}
          {concept.warnings.map((w, i) => <Warn key={i} text={withNames(w, names)} />)}
        </Block>
      )}

      {/* 单笔风险表已经并入上方「持仓速览」——同一份数据不再渲染两遍，
          否则普通投资者要在两张几乎一样的表之间对照，反而看不出重点。 */}

      {/* 情景冲击 */}
      {scenario && (
        <Block title="跌 10% / 20% 会怎样">
          <Hint>做个假设：如果这些票一起跌 10% 或 20%，你的整个账户会缩水多少。表里「-6.00pp」意思是账户总权益会少掉 6 个百分点（负号=变少），金额是大概会亏多少钱。这是线性估算，真跌起来还可能因为同涨同跌被放大。</Hint>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[30rem] text-left text-[11px]">
              <thead className="text-muted">
                <tr>
                  <th className="py-1 font-normal">标的</th>
                  <th className="py-1 text-right font-normal">权重</th>
                  <th className="py-1 text-right font-normal">跌10%</th>
                  <th className="py-1 text-right font-normal">跌20%</th>
                  <th className="py-1 text-right font-normal">金额(10%/20%)</th>
                </tr>
              </thead>
              <tbody>
                {scenario.positions.map(row => (
                  <tr key={row.symbol} className="border-t border-border/50">
                    <td className="py-1"><Sym symbol={row.symbol} names={names} /></td>
                    <td className="py-1 text-right font-mono">{pct(row.weight)}</td>
                    <td className="py-1 text-right font-mono text-danger">
                      {row.equity_impact_pct_10 === null || row.equity_impact_pct_10 === undefined
                        ? '—'
                        : `${row.equity_impact_pct_10.toFixed(2)}pp`}
                    </td>
                    <td className="py-1 text-right font-mono text-danger">
                      {row.equity_impact_pct_20 === null || row.equity_impact_pct_20 === undefined
                        ? '—'
                        : `${row.equity_impact_pct_20.toFixed(2)}pp`}
                    </td>
                    <td className="py-1 text-right font-mono text-secondary">
                      {money(row.amount_10)} / {money(row.amount_20)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px]">
            <Metric
              label="全部持仓跌10%"
              value={`${scenario.total_equity_impact_pct_10?.toFixed(2) ?? '—'}pp`}
              extra={scenario.total_amount_10 !== null && scenario.total_amount_10 !== undefined ? money(scenario.total_amount_10) : undefined}
              warn
            />
            <Metric
              label="全部持仓跌20%"
              value={`${scenario.total_equity_impact_pct_20?.toFixed(2) ?? '—'}pp`}
              extra={scenario.total_amount_20 !== null && scenario.total_amount_20 !== undefined ? money(scenario.total_amount_20) : undefined}
              warn
            />
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-muted">{withNames(scenario.note, names)}</p>
        </Block>
      )}

      {/* 回撤预算 */}
      <Block id="risk-budget" title="整体回撤预算">
        <Hint>给整个组合定一个「最多愿意亏多少」的预算（默认 15%）。把每只票最坏能跌多少按权重加起来，就是「压力回撤」；超过预算时，下面会指名道姓说哪只票该减一点仓。这只是配置上的上界，不是预测。</Hint>
        <Metric
          label="压力回撤"
          value={`${budget.stressed_drawdown_pct.toFixed(2)}%`}
          // budget_usage_pct 后端已 ×100（`round(used * 100, 2)`），是百分数口径，
          // 不能再过 asPct —— 否则用量 0.67%（压力回撤极小）会被放大成 67%。
          extra={`预算 ${budget.budget_pct}% · 用量 ${budget.budget_usage_pct === null ? '—' : `${budget.budget_usage_pct.toFixed(0)}%`}`}
          warn={budget.over_budget}
        />
        <p className="mt-1 text-[11px] leading-relaxed text-muted">
          压力回撤假设所有持仓同时走到各自潜在回撤位，是「最坏能亏多少」的配置上界，
          不是预测，也不保证不亏。
        </p>
        {budget.suggested_cuts.length > 0 && (
          <div className="mt-2 rounded-btn border border-danger/30 bg-danger/5 px-2 py-1.5">
            <p className="text-[11px] text-danger">超预算，建议削减：</p>
            <ul className="mt-1 space-y-0.5 text-[11px]">
              {budget.suggested_cuts.map(cut => (
                <li key={cut.symbol} className="flex justify-between gap-2">
                  <span><Sym symbol={cut.symbol} names={names} /></span>
                  <span className="text-secondary">
                    占比 {pct(cut.current_weight)} → <span className="font-mono">{pct(cut.new_weight)}</span>（减 {pct(cut.reduce_by)}）
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {budget.warnings.map((w, i) => <Warn key={i} text={withNames(w, names)} />)}
      </Block>

      {/* 进阶指标：数据质量门禁 / 今日可执行 / 尾部情景 / 费用穿透 / 相对基准 / 因子归因。
          这 6 块对「我危不危险」的判断贡献很小，但对普通投资者是噪音, 默认收起。
          数据质量仍然是四盏灯之一, 点灯直接跳到这里, 需要时展开看证据。 */}
      {(report.data_quality || (report.actionability?.suggested_cuts ?? []).length > 0
        || report.tail_scenarios || report.fee_passthrough || report.relative_performance
        || report.factor_attribution) && (
        <details id="risk-advanced" className="scroll-mt-4 rounded-btn border border-border bg-surface px-3 py-2">
          <summary className="cursor-pointer text-[11px] font-medium text-foreground">
            进阶指标（数据质量 / 可执行性 / 尾部情景 / 费用 / 基准 / 因子）
            <span className="ml-1 font-normal text-muted">—— 不影响上面结论，想深挖再点开</span>
          </summary>
          <div className="mt-2 space-y-3">
      {report.data_quality && (
        <Block title="数据新鲜度与覆盖（结论可信度）">
          <Hint>先看这份结论用的行情旧不旧、够不够全。数据不新鲜或共同交易日太少时，上面的百分比只能当「参考」，别当成精确结论。</Hint>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            <Metric
              label="数据截至"
              value={report.data_quality.data_through ?? report.data_quality.as_of ?? '—'}
              extra={report.data_quality.as_of ? `评估日 ${report.data_quality.as_of}` : undefined}
            />
            <Metric
              label="共同交易日"
              value={report.data_quality.common_days != null ? `${report.data_quality.common_days} 天` : '—'}
            />
            <Metric
              label="门禁结论"
              value={report.data_quality.verdict === 'ok' ? '可用（正式结论）' : '仅供参考'}
              warn={report.data_quality.verdict !== 'ok'}
            />
          </div>
          {(report.data_quality.flags ?? []).length > 0 && (
            <ul className="mt-1 space-y-0.5 text-[11px] text-warning">
              {(report.data_quality.flags ?? []).slice(0, 6).map((f, i) => <li key={i}>· {withNames(f, names)}</li>)}
            </ul>
          )}
          {(report.data_quality.limitations ?? []).length > 0 && (
            <p className="mt-1 text-[11px] leading-relaxed text-muted">
              {(report.data_quality.limitations ?? []).slice(0, 2).join('；')}
            </p>
          )}
        </Block>
      )}

      {report.actionability && (report.actionability.suggested_cuts ?? []).length > 0 && (
        <Block title="今天实际能减多少（可执行性）">
          <Hint>「建议减仓」是按风险算出来的目标；但停牌、T+1、跌停封板、成交太清淡时今天根本卖不掉或卖不满。这一栏把「该减多少」和「今天能减多少」分开，别把做不到的动作当成已经完成。</Hint>
          <ul className="mt-1 space-y-1 text-[11px]">
            {(report.actionability.suggested_cuts ?? []).map(cut => (
              <li key={cut.symbol} className="flex flex-wrap justify-between gap-x-2 gap-y-0.5">
                <span><Sym symbol={cut.symbol} names={names} /></span>
                <span className="text-secondary">
                  风险目标 减 {pct(cut.reduce_by)} · 今日能减{' '}
                  <span className={cn('font-mono', cut.executable_reduce_by == null ? 'text-warning' : 'text-foreground')}>
                    {cut.executable_reduce_by == null ? '顺延/待核实' : pct(cut.executable_reduce_by)}
                  </span>
                  {cut.actionability ? `（${cut.actionability}）` : ''}
                </span>
              </li>
            ))}
          </ul>
          {(report.actionability.note ?? '') && (
            <p className="mt-1 text-[11px] leading-relaxed text-muted">{report.actionability.note}</p>
          )}
        </Block>
      )}

      {report.tail_scenarios && (
        <Block title="极端行情会怎样（尾部情景）">
          <Hint>普通情景只是「线性近似」：假设各跌各的。真出事时大家会一起跌，相关性越高损失越被放大；下面还给几个历史极端窗口作对照，这些都是压力假设、不是预测。</Hint>
          {report.tail_scenarios.correlation_amplified && (
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              <Metric
                label="相关性放大系数"
                value={report.tail_scenarios.correlation_amplified.amplification_factor != null
                  ? `${report.tail_scenarios.correlation_amplified.amplification_factor.toFixed(2)}×`
                  : '—'}
              />
              <Metric
                label="放大的跌10%损失"
                value={report.tail_scenarios.correlation_amplified.amplified_loss_10pct != null
                  ? `${report.tail_scenarios.correlation_amplified.amplified_loss_10pct.toFixed(2)}%`
                  : '—'}
                warn
              />
            </div>
          )}
          {(report.tail_scenarios.historical_stress ?? []).length > 0 && (
            <ul className="mt-1 space-y-0.5 text-[11px] text-secondary">
              {(report.tail_scenarios.historical_stress ?? []).map((s, i) => (
                <li key={i} className="flex justify-between gap-2">
                  <span className="truncate">{s.label ?? s.date_range ?? '历史窗口'}</span>
                  <span className="shrink-0 font-mono text-danger">
                    {s.equity_drawdown_pct != null ? `${s.equity_drawdown_pct.toFixed(1)}%` : '—'}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {(report.tail_scenarios.limitations ?? []).length > 0 && (
            <p className="mt-1 text-[11px] leading-relaxed text-muted">
              {(report.tail_scenarios.limitations ?? []).slice(0, 2).join('；')}
            </p>
          )}
        </Block>
      )}

      {report.fee_passthrough && (
        <Block title="到手金额（扣费后）">
          <Hint>上面所有盈亏和「跌 10% 会亏多少」都是没扣手续费的毛数。真把持仓卖掉，佣金、印花税、过户费和滑点还要吃掉一小块，这一栏给的是扣费后的事前估算。</Hint>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            <Metric
              label="卖出综合费率"
              value={report.fee_passthrough.sell_cost_pct != null
                ? `${(report.fee_passthrough.sell_cost_pct * 100).toFixed(3)}%`
                : '—'}
            />
            <Metric label="持仓市值(估)" value={report.fee_passthrough.market_value_estimate != null ? money(report.fee_passthrough.market_value_estimate) : '—'} />
            <Metric
              label="全部变现到手(估)"
              value={report.fee_passthrough.net_after_fees_estimate != null ? money(report.fee_passthrough.net_after_fees_estimate) : '—'}
            />
          </div>
          {(report.fee_passthrough.note ?? '') && (
            <p className="mt-1 text-[11px] leading-relaxed text-muted">{report.fee_passthrough.note}</p>
          )}
        </Block>
      )}

      {report.relative_performance && (
        <Block title="相对基准（跑赢还是跑输）">
          <Hint>只看自己赚了多少不够，还要跟大盘比。超额是「比沪深300多赚/少赚多少」，β 是跟着大盘起伏的幅度，信息比率是衡量这份超额稳不稳；样本太少时这些数只作参考。</Hint>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            <Metric label="组合区间收益" value={report.relative_performance.period_return != null ? `${(report.relative_performance.period_return * 100).toFixed(2)}%` : '—'} />
            <Metric label="基准区间收益" value={report.relative_performance.benchmark_return != null ? `${(report.relative_performance.benchmark_return * 100).toFixed(2)}%` : '—'} />
            <Metric
              label="超额收益"
              value={report.relative_performance.excess_return != null ? `${(report.relative_performance.excess_return * 100).toFixed(2)}%` : '—'}
              warn={(report.relative_performance.excess_return ?? 0) < 0}
            />
            <Metric label="β（跟涨跌幅）" value={report.relative_performance.beta != null ? report.relative_performance.beta.toFixed(2) : '—'} />
            <Metric label="α（超额能力）" value={report.relative_performance.alpha != null ? `${(report.relative_performance.alpha * 100).toFixed(2)}%` : '—'} />
            <Metric label="信息比率" value={report.relative_performance.information_ratio != null ? report.relative_performance.information_ratio.toFixed(2) : '—'} />
            <Metric label="共同交易日" value={`${report.relative_performance.common_days ?? 0} 天`} />
          </div>
          {/* 「跑赢」最容易被读反：低 β 组合在大盘下跌时天然少跌, 那不是选股能力强。
              这里把三个数摆到一起直接给结论, 不让用户自己去推。 */}
          {report.relative_performance.excess_return != null && (
            <p className="mt-1 text-[11px] leading-relaxed text-secondary">
              {(() => {
                const rp = report.relative_performance
                const excess = (rp.excess_return ?? 0) * 100
                const period = (rp.period_return ?? 0) * 100
                const bench = (rp.benchmark_return ?? 0) * 100
                const beta = rp.beta
                const lowBeta = beta != null && beta < 0.6
                if (excess > 0 && period < 0 && bench < 0 && lowBeta) {
                  return `组合 ${period.toFixed(2)}% / 基准 ${bench.toFixed(2)}%，超额 +${excess.toFixed(2)}% 是「跌得比大盘少」，`
                    + `不是选股超额（β 仅 ${beta!.toFixed(2)}，本来就跟跌得少）。两边都在亏钱时，别把少亏读成赚钱。`
                }
                if (excess > 0) {
                  return `组合 ${period.toFixed(2)}% / 基准 ${bench.toFixed(2)}%，比基准多 ${excess.toFixed(2)}%。`
                }
                return `组合 ${period.toFixed(2)}% / 基准 ${bench.toFixed(2)}%，比基准少 ${Math.abs(excess).toFixed(2)}%。`
              })()}
            </p>
          )}
          {(report.relative_performance.limitations ?? []).length > 0 && (
            <p className="mt-1 text-[11px] leading-relaxed text-muted">
              {(report.relative_performance.limitations ?? []).slice(0, 2).join('；')}
            </p>
          )}
        </Block>
      )}

      {report.factor_attribution && (
        <Block title="因子风险归因">
          <Hint>把组合的风险拆到「市值、价值、动量、波动、β」这些风格因子上，看亏钱是因为押错了风格还是个股本身。当前没有逐票的因子暴露数据，只如实给覆盖率，不编造拆解。</Hint>
          <Metric
            label="因子覆盖"
            value={report.factor_attribution.coverage
              ? `${report.factor_attribution.coverage.covered}/${report.factor_attribution.coverage.total}`
              : '—'}
            extra="已拿到暴露的标的 / 全部标的"
          />
          {Object.keys(report.factor_attribution.exposures ?? {}).length > 0 && (
            <ul className="mt-1 space-y-0.5 text-[11px] text-secondary">
              {Object.entries(report.factor_attribution.exposures ?? {}).map(([f, v]) => (
                <li key={f} className="flex justify-between gap-2">
                  <span>{f}</span><span className="font-mono text-foreground">{Number(v).toFixed(3)}</span>
                </li>
              ))}
            </ul>
          )}
          {(report.factor_attribution.limitations ?? []).length > 0 && (
            <p className="mt-1 text-[11px] leading-relaxed text-muted">
              {(report.factor_attribution.limitations ?? []).slice(0, 2).join('；')}
            </p>
          )}
        </Block>
      )}
          </div>
        </details>
      )}

      {report.limitations.length > 0 && (
        <details id="risk-limitations" className="scroll-mt-4 rounded-btn bg-elevated/30 px-2 py-1" open>
          <summary className="cursor-pointer text-[10px] text-muted">
            口径与限制（{report.limitations.length}）—— 哪些数据没用上、结论打了折
          </summary>
          <ul className="mt-1 space-y-0.5 text-[10px] leading-relaxed text-muted">
            {report.limitations.slice(0, 4).map((l, i) => <li key={i}>· {withNames(l, names)}</li>)}
          </ul>
          {report.limitations.length > 4 && (
            <details className="mt-1">
              <summary className="cursor-pointer text-[10px] text-muted">
                展开其余 {report.limitations.length - 4} 条
              </summary>
              <ul className="mt-1 space-y-0.5 text-[10px] leading-relaxed text-muted">
                {report.limitations.slice(4).map((l, i) => <li key={i}>· {withNames(l, names)}</li>)}
              </ul>
            </details>
          )}
        </details>
      )}
    </div>
  )
}

function Block({
  title, children, icon: Icon = ShieldAlert, id,
}: { title: string; children: React.ReactNode; icon?: typeof ShieldAlert; id?: string }) {
  return (
    <div id={id} className="scroll-mt-4 rounded-btn border border-border bg-surface px-3 py-2">
      <h4 className="mb-1.5 flex items-center gap-1 text-[11px] font-medium text-foreground">
        <Icon className="h-3 w-3 text-accent" />
        {title}
      </h4>
      {children}
    </div>
  )
}

function Metric({
  label, value, extra, warn,
}: { label: string; value: string; extra?: string; warn?: boolean }) {
  return (
    <div className="flex items-baseline gap-1.5 text-[11px]">
      <span className="shrink-0 text-muted">{label}</span>
      <span className={cn('font-mono', warn ? 'text-danger' : 'text-foreground')}>{value}</span>
      {extra && <span className="text-muted">{extra}</span>}
    </div>
  )
}

function Warn({ text }: { text: string }) {
  return (
    <p className="mt-1 flex items-start gap-1 text-[11px] text-warning">
      <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
      {text}
    </p>
  )
}

/** 流动性档位文案（供信号与组合两处复用）。 */
export function liquidityLabel(bucket: string): string {
  return BUCKET_TEXT[bucket] ?? bucket
}

const DIGEST_LABELS: Record<string, string> = {
  top_industry: '最大行业',
  top_industry_share: '最大行业占比',
  effective_industries: '有效行业数',
  top_concept: '最大概念',
  top_concept_symbols: '概念涉及标的',
  top_concept_weight: '概念合计权重',
  effective_bets: '有效独立注数',
  max_position_weight: '最大单票权重',
  stressed_drawdown_pct: '压力回撤',
  budget_usage_pct: '回撤预算用量',
  over_budget: '是否超预算',
}

function fmtDigest(key: string, value: number | string | boolean | null): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'string') return value || '—'
  if (key === 'stressed_drawdown_pct' || key === 'budget_usage_pct' ||
      key === 'top_industry_share' || key === 'top_concept_weight') {
    return `${value.toFixed(2)}%`
  }
  if (key === 'max_position_weight') return pct(value, 1)
  if (key === 'effective_industries' || key === 'effective_bets') return value.toFixed(1)
  return String(value)
}

/**
 * 买入前仓位模拟 —— 回答「买这一笔之后，组合会不会变差」。
 *
 * 权重按「买入后占账户权益的百分比」录入（不是占现有持仓）。
 * 若所需资金超过当前现金比例，会明确说明差额只能靠卖出其它持仓，
 * 本模拟不替你决定卖哪只。
 */
export function BuySimulationCard({
  account,
  manual = false,
  holdings,
  equity,
  symbol: initialSymbol = '',
  warnObservation = false,
}: {
  account?: string | null
  /** true 时用「我的真实持仓」（手工录入）口径 */
  manual?: boolean
  holdings?: { symbol: string; weight: number }[]
  equity?: number | null
  symbol?: string
  warnObservation?: boolean
}) {
  const [symbol, setSymbol] = useState(initialSymbol)
  const [weightPct, setWeightPct] = useState('5')
  const [submitted, setSubmitted] = useState<{ symbol: string; weight: number } | null>(null)

  const w = Number(weightPct)
  const valid = symbol.trim().length > 0 && Number.isFinite(w) && w > 0 && w <= 100

  const useManual = manual && !account
  const query = useQuery({
    queryKey: ['buy-simulation', account ?? '', useManual ? 'manual' : '', submitted?.symbol ?? '', submitted?.weight ?? 0, holdings, equity ?? null],
    queryFn: () =>
      api.buySimulation({
        symbol: submitted!.symbol,
        new_weight_pct: submitted!.weight,
        account: account || undefined,
        source: useManual ? 'manual' : undefined,
        holdings,
        equity: equity ?? undefined,
        budget_pct: 15,
        lookback_days: 60,
      }),
    enabled: Boolean(submitted) && (Boolean(account) || useManual || Boolean(holdings?.length)),
    staleTime: 0,
    gcTime: 60_000,
    retry: false,
  })

  const run = () => {
    if (!valid) return
    setSubmitted({ symbol: symbol.trim().toUpperCase(), weight: w })
  }

  const report = query.data
  // 模拟结果里也只有代码；用同一个批量接口补名称，投资者才知道"买的到底是哪只"。
  const simNames = useInstrumentNames(
    useMemo(() => {
      const s = new Set<string>()
      if (submitted?.symbol) s.add(submitted.symbol)
      if (report?.new_symbol) s.add(report.new_symbol)
      return [...s]
    }, [submitted?.symbol, report?.new_symbol]),
  ).data?.names ?? {}
  const rows: { key: string; before: string; after: string; delta: string; worse: boolean }[] = []
  if (report) {
    for (const key of Object.keys(report.after)) {
      const before = report.before[key] ?? null
      const after = report.after[key] ?? null
      const delta = report.deltas[key] ?? null
      const numDelta = typeof delta === 'number' ? delta : null
      // 「越大越差」的字段: 集中度/权重/回撤/用量; effective_* 与 top_*_symbols 反向
      const higherIsWorse = !key.startsWith('effective_') && key !== 'top_concept_symbols'
      const worse = numDelta !== null && (higherIsWorse ? numDelta > 0 : numDelta < 0)
      rows.push({
        key,
        before: fmtDigest(key, before as number | string | boolean | null),
        after: fmtDigest(key, after as number | string | boolean | null),
        delta: numDelta === null ? '—' : `${numDelta > 0 ? '+' : ''}${numDelta.toFixed(2)}`,
        worse,
      })
    }
  }

  return (
    <div className="rounded-btn border border-border bg-surface px-3 py-2">
      <h4 className="mb-1.5 flex items-center gap-1 text-[11px] font-medium text-foreground">
        <Wallet className="h-3 w-3 text-accent" />
        买入前仓位模拟
        <span className="ml-1 font-normal text-muted">买这一笔之后，组合会不会变差</span>
      </h4>
      <Hint>先填打算买的票和「买完占账户多少比例」，再点模拟。下面把买入前 / 买入后的集中度、回撤等指标并排摆出来；变红的那一列就是这一笔买入会让情况变差的项，方便你决定要不要买、买多少。</Hint>

      <div className="flex flex-wrap items-center gap-2">
        <input
          value={symbol}
          onChange={e => setSymbol(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') run() }}
          placeholder="代码，如 600941.SH（中国移动）"
          aria-label="模拟买入的标的代码"
          className="w-36 rounded-btn border border-border bg-elevated/40 px-2 py-1 font-mono text-[11px] text-foreground outline-none focus:border-accent/60"
        />
        <label className="flex items-center gap-1 text-[11px] text-muted">
          买入后占总权益
          <input
            value={weightPct}
            onChange={e => setWeightPct(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') run() }}
            inputMode="decimal"
            aria-label="买入后占账户权益百分比"
            className="w-14 rounded-btn border border-border bg-elevated/40 px-1.5 py-1 text-right font-mono text-[11px] text-foreground outline-none focus:border-accent/60"
          />
          %
        </label>
        <button
          onClick={run}
          disabled={!valid || query.isFetching}
          className="rounded-btn border border-accent/40 bg-accent/10 px-2.5 py-1 text-[11px] text-accent cursor-pointer disabled:cursor-not-allowed disabled:opacity-40"
        >
          {query.isFetching ? '模拟中…' : '模拟'}
        </button>
        {!valid && weightPct !== '' && (
          <span className="text-[10px] text-muted">权重需在 0~100 之间</span>
        )}
      </div>

      {warnObservation && (
        <p className="mt-1 text-[10px] leading-relaxed text-warning">
          观察口径下这里假设的是等权组合，不是你的实际持仓。
        </p>
      )}

      {query.error && (
        <p role="alert" className="mt-1 text-[11px] text-danger">{(query.error as Error).message}</p>
      )}

      {report && (
        <div className="mt-2 space-y-1.5">
          <p className="text-[11px] text-secondary">
            本次模拟：<span className="text-foreground">{symLabel(report.new_symbol, simNames)}</span>
            ，买完约占账户 <span className="font-mono text-foreground">{report.new_weight_pct}%</span>
          </p>
          {report.over_cash && (
            <p className="flex items-start gap-1 rounded-btn border border-warning/30 bg-warning/[0.06] px-2 py-1 text-[10px] leading-relaxed text-warning">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
              买入 {symLabel(report.new_symbol, simNames)} {report.new_weight_pct}% 超过当前现金占比 {report.cash_weight_before_pct}% —— 差额必须来自卖出其它持仓，本模拟不替你决定卖哪只。
            </p>
          )}

          <div className="overflow-x-auto">
            <table className="w-full min-w-[26rem] text-left text-[11px]">
              <thead className="text-muted">
                <tr>
                  <th className="py-1 font-normal">指标</th>
                  <th className="py-1 text-right font-normal">买入前</th>
                  <th className="py-1 text-right font-normal">买入后</th>
                  <th className="py-1 text-right font-normal">变化</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(r => (
                  <tr key={r.key} className="border-t border-border/50">
                    <td className="py-1 text-secondary">{DIGEST_LABELS[r.key] ?? r.key}</td>
                    <td className="py-1 text-right font-mono text-secondary">{r.before}</td>
                    <td className={cn('py-1 text-right font-mono', r.worse ? 'text-danger' : 'text-foreground')}>
                      {r.after}
                    </td>
                    <td className={cn('py-1 text-right font-mono', r.worse ? 'text-danger' : 'text-muted')}>
                      {r.delta}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {report.worst_case_loss !== null && (
            <Metric
              label="最坏情境亏损"
              value={money(report.worst_case_loss)}
              extra={`按压力回撤 ${fmtDigest('stressed_drawdown_pct', report.after.stressed_drawdown_pct ?? null)} 估算，不是预测`}
              warn
            />
          )}

          {report.warnings.map((t, i) => <Warn key={i} text={withNames(t, simNames)} />)}
          {report.limitations.length > 0 && (
            <details className="rounded-btn bg-elevated/30 px-2 py-1">
              <summary className="cursor-pointer text-[10px] text-muted">
                口径与限制（{report.limitations.length}）
              </summary>
              <ul className="mt-1 space-y-0.5 text-[10px] leading-relaxed text-muted">
                {report.limitations.map((l, i) => <li key={i}>· {withNames(l, simNames)}</li>)}
              </ul>
            </details>
          )}
        </div>
      )}
    </div>
  )
}
