import { AlertTriangle, Info, Loader2, ShieldCheck, TrendingUp } from 'lucide-react'
import type { SignalRisk } from '@/lib/api'

const VERDICT_TEXT: Record<string, { label: string; className: string; hint: string }> = {
  trusted: {
    label: '样本充足',
    className: 'text-bear',
    hint: '样本量达到门限，胜率区间可读',
  },
  insufficient: {
    label: '样本不足',
    className: 'text-warning',
    hint: '样本量偏小，胜率区间过宽，仅作参考',
  },
  no_samples: {
    label: '无样本',
    className: 'text-danger',
    hint: '本地历史窗口内没有可评估的触发样本',
  },
}

const LIQUIDITY_TEXT: Record<string, string> = {
  high: '充足（日均成交额 ≥10 亿）',
  mid: '中等（1~10 亿）',
  low: '偏薄（<1 亿）',
  unknown: '未知',
}

function pct(value: number | null, digits = 1): string {
  if (value === null || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

/** 分数与胜率是两个不同的东西 —— 这里刻意分开渲染，并各自带说明。 */
function ScoreAndWinRate({ risk }: { risk: SignalRisk }) {
  const ci = risk.confidence_interval
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div className="rounded-btn border border-accent/30 bg-accent/5 px-3 py-2">
        <div className="flex items-baseline gap-1">
          <TrendingUp className="h-3.5 w-3.5 text-accent" />
          <span className="text-[11px] text-secondary">信号分数</span>
          <span className="ml-auto text-lg font-semibold text-accent">{risk.signal_score.value}</span>
          <span className="text-[11px] text-muted">/100</span>
        </div>
        <p className="mt-1 text-[11px] leading-relaxed text-secondary">
          {risk.signal_score.meaning}
        </p>
        {risk.signal_score.components && (
          <p className="mt-0.5 font-mono text-[10px] text-muted">
            {risk.signal_score.components.join(' + ')}
          </p>
        )}
      </div>

      <div className="rounded-btn border border-border bg-surface px-3 py-2">
        <div className="flex items-baseline gap-1">
          <span className="text-[11px] text-secondary">历史胜率</span>
          <span className="ml-auto text-lg font-semibold text-foreground">{pct(risk.win_rate)}</span>
        </div>
        <p className="mt-1 font-mono text-[11px] text-secondary">
          {ci.level * 100}% 区间 {pct(ci.low)} ~ {pct(ci.high)}
          <span className="ml-1 text-muted">（{ci.method}，n={risk.samples}）</span>
        </p>
        <p className="mt-0.5 text-[11px] text-muted">
          胜率是历史频率，不是对未来概率的承诺；区间越宽越不可信
        </p>
      </div>
    </div>
  )
}

export function SignalRiskCard({
  risk,
  loading,
  error,
}: {
  risk?: SignalRisk
  loading?: boolean
  error?: Error | null
}) {
  if (loading) {
    return (
      <p className="flex items-center gap-2 px-1 py-2 text-[11px] text-secondary">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />正在计算信号统计…
      </p>
    )
  }
  if (error) {
    return (
      <p role="alert" className="px-1 py-2 text-[11px] text-danger">
        {error.message}
      </p>
    )
  }
  if (!risk) return null

  const verdict = VERDICT_TEXT[risk.verdict] ?? VERDICT_TEXT.no_samples
  const hasSamples = risk.samples > 0

  return (
    <div className="space-y-3 rounded-btn border border-border bg-base px-3 py-3">
      <ScoreAndWinRate risk={risk} />

      <div className="grid gap-x-4 gap-y-1 text-[11px] sm:grid-cols-2">
        <Row label="理由" value={risk.reason || '未填写'} muted={!risk.reason} />
        <Row label="反证条件" value={risk.falsify || '未填写'} muted={!risk.falsify} />
        <Row
          label="预计持有周期"
          value={`${risk.expected_holding_days} 个交易日`}
        />
        <Row label="流动性" value={LIQUIDITY_TEXT[risk.liquidity.bucket] ?? risk.liquidity.bucket} />
        <Row
          label="潜在回撤"
          value={
            risk.potential_drawdown === null
              ? '—'
              : `${pct(risk.potential_drawdown)}（5% 分位，最差 ${pct(risk.worst)}）`
          }
        />
        <Row
          label="仓位上限"
          value={`${risk.position_cap.pct}% · ${risk.position_cap.basis}`}
        />
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border pt-2 text-[11px]">
        <span className={verdict.className}>
          {verdict.label}
        </span>
        <span className="text-muted">{verdict.hint}</span>
        <span className="text-secondary">
          超额均值 <span className="font-mono text-foreground">{pct(risk.excess_mean, 2)}</span>
        </span>
        <span className="text-secondary">
          样本 <span className="font-mono text-foreground">{risk.samples}</span>
        </span>
        {risk.checked_window.from && (
          <span className="text-muted">
            {risk.checked_window.from} ~ {risk.checked_window.to}
          </span>
        )}
      </div>

      {!hasSamples && (
        <p className="flex items-start gap-1.5 rounded-btn border border-warning/30 bg-warning/5 px-2 py-1.5 text-[11px] text-warning">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          没有样本时，分数与胜率都没有意义：不要把它读成「0 分 = 差」或「0% = 不会涨」。
        </p>
      )}

      {risk.limitations.length > 0 && (
        <ul className="space-y-0.5 text-[10px] leading-relaxed text-muted">
          {risk.limitations.map((l, i) => (
            <li key={i} className="flex items-start gap-1">
              <Info className="mt-0.5 h-2.5 w-2.5 shrink-0" />
              {l}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Row({ label, value, muted }: { label: string; value: string; muted?: boolean }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="shrink-0 text-muted">{label}</span>
      <span className={`min-w-0 break-words ${muted ? 'text-muted' : 'text-foreground'}`}>{value}</span>
    </div>
  )
}

/** 一条信号未展开时的紧凑徽标：只显示分数与样本量，避免喧宾夺主。 */
export function SignalRiskBadge({
  risk,
  onClick,
}: {
  risk?: SignalRisk
  onClick?: () => void
}) {
  if (!risk) {
    return (
      <button
        onClick={onClick}
        className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[10px] text-muted hover:bg-elevated hover:text-foreground"
      >
        <ShieldCheck className="h-2.5 w-2.5" />查看风险画像
      </button>
    )
  }
  const verdict = VERDICT_TEXT[risk.verdict] ?? VERDICT_TEXT.no_samples
  return (
    <button
      onClick={onClick}
      title={`胜率 ${pct(risk.win_rate)}（n=${risk.samples}，区间 ${pct(risk.confidence_interval.low)}~${pct(risk.confidence_interval.high)}）`}
      className="inline-flex items-center gap-1.5 rounded border border-border px-1.5 py-0.5 text-[10px] hover:bg-elevated"
    >
      <span className="text-accent">分数 {risk.signal_score.value}</span>
      <span className="text-muted">·</span>
      <span className="text-foreground">胜率 {pct(risk.win_rate)}</span>
      <span className="text-muted">n={risk.samples}</span>
      <span className={verdict.className}>{verdict.label}</span>
    </button>
  )
}
