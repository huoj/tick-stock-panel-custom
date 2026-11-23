import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { AlertTriangle, BarChart3, CheckCircle2, RefreshCw, Star } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fetchDailyBatchChunked } from '@/lib/klineBatchChunked'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { cn } from '@/lib/cn'
import { PageHeader } from '@/components/PageHeader'
import { Skeleton } from '@/components/data/Skeleton'
import { ManualHoldingsPanel } from './ManualHoldingsPanel'
import { StateActionDialog } from './StateActionDialog'
import { PlainGuide, POSITION_OVERVIEW_TERMS } from './PlainGuide'

const STATE_STYLE: Record<string, string> = {
  watch: 'bg-muted/15 text-muted border-muted/30',
  planning: 'bg-warning/15 text-warning border-warning/30',
  holding: 'bg-accent/15 text-accent border-accent/30',
}

const VERDICT_STYLE: Record<string, { label: string; cls: string }> = {
  validated: { label: '有效', cls: 'text-success' },
  weak_signal: { label: '弱有效', cls: 'text-warning' },
  inverted: { label: '反向指标', cls: 'text-danger' },
  no_edge: { label: '无预测力', cls: 'text-muted' },
  insufficient: { label: '样本不足', cls: 'text-muted' },
}

function money(v: number | null | undefined): string {
  if (v == null) return '—'
  return v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

/** 规则有效性面板 —— 这张卡的价值在于"敢说规则没用"。 */
function RuleValidationPanel({ symbol }: { symbol: string }) {
  const [step, setStep] = useState(3)
  const q = useQuery({
    queryKey: ['strength-validation', symbol, step],
    queryFn: () => api.validateStrength(symbol, step),
    enabled: !!symbol,
  })

  if (!symbol) return null
  if (q.isLoading) return <Skeleton className="h-24 w-full" />
  if (q.error) {
    return (
      <div className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-[11px] text-danger">
        规则检验失败：{(q.error as Error).message}
      </div>
    )
  }
  const d = q.data
  if (!d) return null

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <BarChart3 className="h-3.5 w-3.5 text-muted" />
        <span className="text-xs font-medium">强弱判定的历史有效性</span>
        <span className="text-[10px] text-muted">采样 {d.sampled_days} 天 · 步长 {step}</span>
        <div className="ml-auto flex gap-1">
          {[1, 3, 10].map(s => (
            <button key={s} onClick={() => setStep(s)}
              className={cn('rounded px-1.5 py-0.5 text-[10px]',
                step === s ? 'bg-accent text-white' : 'border border-border/60 text-muted')}>
              {s}天
            </button>
          ))}
        </div>
      </div>
      <div className="rounded-btn border border-border/50 px-3 py-2 space-y-1.5">
        <div className="text-[10px] text-muted">
          基准（任意一天之后）：
          {Object.entries(d.baseline).map(([w, v]) => {
            const s = v as { win_rate?: number }
            return <span key={w} className="ml-2">{w}日 {s.win_rate ?? '—'}%</span>
          })}
        </div>
        {d.groups.map(g => {
          const st = VERDICT_STYLE[g.verdict] ?? VERDICT_STYLE.no_edge
          return (
            <div key={g.stance} className="border-t border-border/40 pt-1">
              <div className="flex items-baseline gap-2 text-[11px]">
                <span className="text-secondary">{g.label}</span>
                <span className={cn('font-medium', st.cls)}>{st.label}</span>
                <span className="text-muted">n={g.samples}</span>
                <span className="ml-auto font-mono text-[10px] text-muted">
                  {Object.entries(g.forward).map(([w, v]) => {
                    const s = v as { win_rate?: number }
                    return <span key={w} className="ml-1.5">{w}日 {s.win_rate ?? '—'}%</span>
                  })}
                </span>
              </div>
              {g.note && <div className="text-[10px] leading-4 text-muted">{g.note}</div>}
            </div>
          )
        })}
        {d.conclusions.length > 0 && (
          <div className="border-t border-border/40 pt-1 space-y-0.5">
            {d.conclusions.map((c, i) => (
              <div key={i} className={cn('text-[10px] leading-4',
                c.includes('反向') ? 'text-danger' : 'text-secondary')}>
                {c}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

/** 对账面板。 */
function ReconcilePanel({ symbol }: { symbol: string }) {
  const q = useQuery({
    queryKey: ['plan-reconcile', symbol],
    queryFn: () => api.planReconcile(symbol),
    enabled: !!symbol,
    retry: false,
  })
  if (q.error) return null   // 没有计划时后端 404, 不当作错误展示
  if (!q.data) return null
  const d = q.data
  if (!d.matches.length) return null

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1.5 text-xs font-medium">
        <RefreshCw className="h-3 w-3 text-muted" />计划 vs 实际成交
        <span className="text-[10px] font-normal text-muted">
          已匹配 {d.matched_count} / 未匹配 {d.unmatched_batches}
          {d.orphan_fills > 0 && ` / 计划外成交 ${d.orphan_fills}`}
        </span>
      </div>
      <div className="rounded-btn border border-border/50 px-3 py-2 space-y-0.5">
        {d.matches.map((m, i) => (
          <div key={i} className="flex items-baseline gap-2 text-[10px] leading-4">
            {m.status === 'matched'
              ? <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0 text-success" />
              : <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-muted" />}
            <span className="text-secondary">{m.batch_label}</span>
            <span className="font-mono">
              计划 {m.batch_price.toFixed(2)}
              {m.fill_price != null && ` → 成交 ${m.fill_price.toFixed(2)}`}
            </span>
            {m.price_diff_pct != null && (
              <span className={cn('font-mono', Math.abs(m.price_diff_pct) > 2 ? 'text-warning' : 'text-muted')}>
                {m.price_diff_pct > 0 ? '+' : ''}{m.price_diff_pct}%
              </span>
            )}
          </div>
        ))}
        {d.warnings.map((w, i) => (
          <div key={i} className="text-[10px] leading-4 text-warning">! {w}</div>
        ))}
      </div>
    </div>
  )
}

export function PositionOverview() {
  const [input, setInput] = useState('')
  const [symbols, setSymbols] = useState<string[]>([])
  // 单票检验与三态分类是两个独立输入 —— 共用同一份 state 会让「打字没反应」
  const [ruleInput, setRuleInput] = useState('')
  const [ruleSymbol, setRuleSymbol] = useState('')
  const [imported, setImported] = useState(false)
  const [importTruncated, setImportTruncated] = useState(0)
  // 行内状态写回: 打开哪个弹窗 / 哪只票正在二次确认移除持仓。
  const [dialog, setDialog] = useState<{ symbol: string; kind: 'plan' | 'hold' } | null>(null)
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null)
  const [actionErr, setActionErr] = useState<string | null>(null)
  const qc = useQueryClient()

  // 没有手动输入代码时，默认把已录入的真实持仓带进三态分类 ——
  // 否则用户录完持仓，下面「按代码查看状态」仍然是空的，会以为没生效。
  const holdings = useQuery({
    queryKey: QK.manualHoldings,
    queryFn: () => api.manualHoldings(),
  })

  const effectiveSymbols = useMemo(() => {
    if (symbols.length > 0) return symbols
    return (holdings.data?.positions ?? []).map(p => p.symbol)
  }, [symbols, holdings.data])

  const classify = useQuery({
    queryKey: [...QK.positionPlans, 'classify', effectiveSymbols.join(',')],
    queryFn: () => api.positionClassify(effectiveSymbols),
    enabled: effectiveSymbols.length > 0,
  })

  // 三态清单原来只有代码 + 「计盈亏」，看不出这是什么票、现在什么价 ——
  // 导入自选后尤其像「什么都没发生」。这里补名称与最近收盘/涨跌幅，
  // 数据源都是既有端点: 名称走 instruments/names, 价格走 daily-batch 分块助手。
  // 只读展示, 不进盈亏/权重, 不改变三态归类。
  const names = useQuery({
    queryKey: QK.instrumentNames(effectiveSymbols),
    queryFn: () => api.instrumentNames(effectiveSymbols),
    enabled: effectiveSymbols.length > 0,
    staleTime: 300_000,
  })

  // days=5: 后端 daily-batch 把 days 钳到 [5,60], 取两根就能算涨跌幅,
  // 多留几根是为了停牌/新股时最后两根仍有可比对象。
  const quotes = useQuery({
    queryKey: QK.positionOverviewQuotes(effectiveSymbols),
    queryFn: () => fetchDailyBatchChunked(effectiveSymbols, 5),
    enabled: effectiveSymbols.length > 0,
    staleTime: 60_000,
  })

  const nameBySymbol = names.data?.names ?? {}

  // 每只取最后两根收盘: 现价 = 最后一根, 涨跌幅 = 后/前 - 1 (比例口径, 交给 fmtPct)。
  // 少于两根、或收盘缺失/非数值 → 不猜, 渲染成「—」。
  const quoteBySymbol = useMemo(() => {
    const out: Record<string, { price: number; pct: number | null }> = {}
    const data = quotes.data?.data ?? {}
    for (const [symbol, rows] of Object.entries(data)) {
      if (!rows?.length) continue
      const last = rows[rows.length - 1]
      const lastClose = last?.close == null ? null : Number(last.close)
      if (lastClose == null || !Number.isFinite(lastClose)) continue
      const prev = rows.length >= 2 ? rows[rows.length - 2] : null
      const prevClose = prev?.close == null ? null : Number(prev.close)
      const pct = prevClose != null && Number.isFinite(prevClose) && prevClose !== 0
        ? lastClose / prevClose - 1
        : null
      out[symbol] = { price: lastClose, pct }
    }
    return out
  }, [quotes.data])

  const capital = useQuery({
    queryKey: [...QK.positionPlans, 'capital'],
    queryFn: () => api.planCapitalCheck(),
  })

  const grouped = useMemo(() => {
    const items = classify.data?.items ?? []
    return {
      holding: items.filter(i => i.state === 'holding'),
      planning: items.filter(i => i.state === 'planning'),
      watch: items.filter(i => i.state === 'watch'),
    }
  }, [classify.data])

  const apply = () => {
    // 不强制大写: 名称(如「美的集团」)与裸代码都交给后端解析
    const list = input.split(/[,，;；\s]+/).map(s => s.trim()).filter(Boolean)
    setSymbols(Array.from(new Set(list)))
  }

  const applyRule = () => {
    const one = ruleInput.split(/[,，;；\s]+/).map(s => s.trim()).filter(Boolean)[0] ?? ''
    setRuleSymbol(one)
  }

  // 「导入自选」只是把自选清单带进三态查看，不写入真实持仓、不计入盈亏。
  // 这是诚实边界：自选观察与计划买入永远不参与盈亏/权重。
  // 基于 effectiveSymbols 合并（不是 symbols）—— 否则录入的真实持仓会被导入覆盖掉。
  const importWatchlist = useMutation({
    mutationFn: () => api.watchlistList(),
    onSuccess: (data) => {
      const list = (data.symbols ?? []).map(e => e.symbol).filter(Boolean)
      // 后端 classify 一次最多 500 个标的，超出会整体 400 —— 前端先截断并如实说明
      const merged = Array.from(new Set([...effectiveSymbols, ...list]))
      const capped = merged.slice(0, 500)
      setImportTruncated(merged.length - capped.length)
      setInput(capped.join(', '))
      setSymbols(capped)
      setImported(true)
    },
  })

  // 状态写回后必须把「三态分类 / 真实持仓 / 组合风险 / 模拟盘买入」一起失效 ——
  // 与 ManualHoldingsPanel.refresh() 用的是同一套键（组合风险的 key 前缀是
  // [...QK.customSignals, 'portfolio-risk']，单独 invalidate ['portfolio-risk'] 匹配不到）。
  // 再补 QK.actionCard(symbol)：行动卡页也读同一份计划/持仓，别让它停在旧值。
  const onWritten = (symbol: string) => {
    setActionErr(null)
    void qc.invalidateQueries({ queryKey: QK.manualHoldings })
    void qc.invalidateQueries({ queryKey: QK.positionPlans })
    void qc.invalidateQueries({ queryKey: [...QK.customSignals, 'portfolio-risk'] })
    void qc.invalidateQueries({ queryKey: ['buy-simulation'] })
    void qc.invalidateQueries({ queryKey: QK.actionCard(symbol) })
    void capital.refetch()
  }

  const removePlan = useMutation({
    mutationFn: (symbol: string) => api.positionPlanDelete(symbol),
    onSuccess: (_data, symbol) => onWritten(symbol),
    onError: (e: Error) => setActionErr(e.message),
  })

  const removeHolding = useMutation({
    mutationFn: (symbol: string) => api.manualHoldingDelete(symbol),
    onSuccess: (_data, symbol) => { setConfirmRemove(null); onWritten(symbol) },
    onError: (e: Error) => setActionErr(e.message),
  })

  return (
    <div className="tsp-readable tsp-overview flex h-full flex-col">
      <PageHeader title="持仓总览" subtitle="先看清手里有什么、哪些真的算数 · 只有实际持仓计入盈亏" />
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <div className="mx-auto max-w-4xl space-y-5">

          {/* 这一页信息密、名词多：先把「这页能帮我判断什么」和黑话翻译摊开。
              用同一个 PlainGuide 渲染器，词表是本页专属，不波及行动卡/组合风控。 */}
          <PlainGuide
            intro="这一页是帮你把「手里的票」理清楚，不是荐股：先把真正买进来的和只是关注/打算买的分开，再看每只票的成本、市值、占账户多少、账面赚亏多少，最后检查计划里的钱够不够用。下面这些词第一次看不懂，点开「其余名词的白话解释」有逐条大白话。"
            terms={POSITION_OVERVIEW_TERMS}
          />

          {/* 真实持仓 —— P0: 不玩模拟盘也能做组合分析的前提 */}
          <ManualHoldingsPanel onChanged={() => { void capital.refetch() }} />

          {/* 资金可行性 */}
          <section className="border-t border-border/40 pt-4">
            <div className="mb-0.5 text-base font-semibold">计划资金校验</div>
            <p className="mb-2 text-xs leading-relaxed text-muted">
              把「打算买」的钱加起来，跟账户里能动的现金比一比：够不够全买，还差多少。只是计划金额，不代表已经下单。
            </p>
            {capital.isLoading && <Skeleton className="h-20 w-full" />}
            {capital.data && (
              <div className="rounded-btn border border-border/50 px-3 py-3 space-y-2">
                <div className="grid grid-cols-3 gap-2">
                  <div>
                    <div className="text-xs text-muted">计划合计</div>
                    <div className="font-mono text-base font-medium">{money(capital.data.total_planned)} 元</div>
                  </div>
                  <div>
                    <div className="text-xs text-muted">已执行</div>
                    <div className="font-mono text-base font-medium">{money(capital.data.total_executed)} 元</div>
                  </div>
                  <div>
                    <div className="text-xs text-muted">待执行</div>
                    <div className="font-mono text-base font-medium">{money(capital.data.total_remaining)} 元</div>
                  </div>
                </div>
                {capital.data.available_cash != null && (
                  <div className="text-xs text-muted">
                    可用现金 {money(capital.data.available_cash)} 元
                    {capital.data.over_budget && (
                      <span className="ml-2 text-danger">
                        缺口 {money(capital.data.shortfall)} 元 —— 全部触发时部分批次无法成交
                      </span>
                    )}
                  </div>
                )}
                {capital.data.warnings.map((w, i) => (
                  <div key={i} className="text-xs leading-relaxed text-warning">! {w}</div>
                ))}
                {/* 逐票明细是次要信息，默认收起，别让页面一打开就一大片小字。 */}
                {capital.data.per_symbol.length > 0 && (
                  <details className="tsp-compact border-t border-border/40 pt-2">
                    <summary className="cursor-pointer text-xs text-secondary">
                      展开逐票明细（{capital.data.per_symbol.length} 只）
                    </summary>
                    <div className="mt-1.5 space-y-1">
                      {capital.data.per_symbol.map(p => (
                        <div key={p.symbol} className="flex flex-wrap items-baseline gap-x-2 text-xs">
                          <span className="font-mono text-secondary">{p.symbol}</span>
                          <span className="text-muted">{p.batches} 批</span>
                          <span className="ml-auto font-mono">
                            计划 {money(p.planned)} / 已执行 {money(p.executed)} / 待执行 {money(p.remaining)}
                          </span>
                        </div>
                      ))}
                    </div>
                  </details>
                )}
              </div>
            )}
          </section>

          {/* 三态分类 */}
          <section className="border-t border-border/40 pt-4">
            <div className="mb-0.5 flex items-baseline gap-2">
              <span className="text-base font-semibold">按状态看清每一只票</span>
              {symbols.length === 0 && (holdings.data?.positions?.length ?? 0) > 0 && (
                <span className="text-xs text-muted">
                  已自动带入上面录入的 {holdings.data!.positions.length} 只真实持仓
                </span>
              )}
            </div>
            <p className="mb-2 text-xs leading-relaxed text-muted">
              把代码或名称填进来，系统会告诉你它现在属于「自选观察 / 计划买入 / 实际持仓」哪一类，并顺手带上名称、现价和涨跌。
            </p>
            <div className="flex gap-2">
              <input value={input} onChange={e => setInput(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') apply() }}
                placeholder="代码或名称，逗号分隔，如 600941.SH, 美的集团"
                className="flex-1 rounded-btn border border-border/60 bg-transparent px-3 py-2 text-sm" />
              <button onClick={apply} className="rounded-btn bg-accent px-4 py-2 text-sm text-white">分类</button>
              <button data-testid="btn-import-watchlist" onClick={() => importWatchlist.mutate()} disabled={importWatchlist.isPending}
                title="把自选清单带进来查看状态；自选本身不等于持仓，不会计入盈亏"
                className="flex items-center gap-1.5 rounded-btn border border-border/60 px-3 py-2 text-sm text-secondary hover:bg-elevated disabled:opacity-50">
                <Star size={13} />
                {importWatchlist.isPending ? '导入中…' : '导入自选'}
              </button>
            </div>
            {imported && (
              <div className="mt-2 text-xs leading-relaxed text-muted">
                已带入自选清单。自选仅为「观察」，不会计入盈亏与权重；真正买入的股票请用上方「我的真实持仓」录入。
                {importTruncated > 0 && (
                  <span className="ml-1 text-warning">
                    （后端一次最多 500 只，另有 {importTruncated} 只未带入）
                  </span>
                )}
                <button onClick={() => {
                  setImported(false)
                  setImportTruncated(0)
                  setSymbols([])
                  setInput('')
                }} className="ml-1 text-accent underline">清空</button>
              </div>
            )}
            {importWatchlist.isError && (
              <div className="mt-2 text-xs text-danger">自选读取失败：{(importWatchlist.error as Error).message}</div>
            )}
          </section>

          {(classify.data?.unresolved?.length ?? 0) > 0 && (
            <div className="rounded-btn border border-warning/40 bg-warning/5 px-3 py-2 space-y-1">
              {classify.data!.unresolved!.map((msg, i) => (
                <div key={i} className="text-[11px] leading-5 text-warning">{msg}</div>
              ))}
            </div>
          )}

          {classify.isLoading && <Skeleton className="h-32 w-full" />}
          {classify.error && (
            <div className="rounded-btn border border-danger/40 bg-danger/5 px-3 py-2 text-xs text-danger">
              {(classify.error as Error).message}
            </div>
          )}

          {classify.data && (
            <div className="space-y-4">
              {(['holding', 'planning', 'watch'] as const).map(state => {
                const items = grouped[state]
                if (!items.length) return null
                const first = items[0]
                return (
                  <div key={state}>
                    <div className="mb-1.5 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      <span className={cn('rounded border px-2 py-0.5 text-sm font-medium', STATE_STYLE[state])}>
                        {first.state_label} · {items.length} 只
                      </span>
                      <span className="text-xs text-muted">
                        {first.counts_toward_pnl ? '计入盈亏与权重' : first.state_note}
                      </span>
                    </div>
                    <div className="rounded-btn border border-border/50 divide-y divide-border/30">
                      {/* 列头：原来一排数字没有表头，普通投资者不知道哪列是钱、哪列是涨跌。
                          用与信息行不同的 className，避免撞上展示测试里的精确定位。 */}
                      <div className="tsp-compact flex items-center gap-2 border-b border-border/40 bg-muted/5 px-3 py-1.5 text-muted" data-testid="list-header">
                        <span className="w-[5.5rem] shrink-0">代码</span>
                        <span className="min-w-0 flex-1">名称</span>
                        <span className="w-16 shrink-0 text-right">现价</span>
                        <span className="w-16 shrink-0 text-right">涨跌</span>
                        <span className="w-14 shrink-0 text-right">盈亏</span>
                        <span className="shrink-0">操作</span>
                      </div>
                      {items.map(it => {
                        const q = quoteBySymbol[it.symbol]
                        return (
                          <div key={it.symbol} data-testid={`row-${it.symbol}`}>
                            {/* 信息行 className 保持不变: A 的展示测试按它精确定位。 */}
                            <div className="flex items-center gap-2 px-3 py-1.5" data-testid={`info-${it.symbol}`}>
                              <span className="w-[5.5rem] shrink-0 font-mono text-xs">{it.symbol}</span>
                              <span className="min-w-0 flex-1 truncate text-xs text-secondary" data-testid={`name-${it.symbol}`}>
                                {nameBySymbol[it.symbol] ?? '—'}
                              </span>
                              <span className="w-16 shrink-0 text-right font-mono text-xs tabular-nums" data-testid={`price-${it.symbol}`}>
                                {q ? fmtPrice(q.price) : '—'}
                              </span>
                              <span className={cn('w-16 shrink-0 text-right font-mono text-xs tabular-nums',
                                priceColorClass(q?.pct ?? null))} data-testid={`pct-${it.symbol}`}>
                                {q ? fmtPct(q.pct) : '—'}
                              </span>
                              <span className={cn('w-14 shrink-0 text-right text-[10px]',
                                it.counts_toward_pnl ? 'text-accent' : 'text-muted')}>
                                {it.counts_toward_pnl ? '计盈亏' : '不计盈亏'}
                              </span>
                              <Link
                                to={`/action-card?symbol=${encodeURIComponent(it.symbol)}`}
                                className="shrink-0 text-[10px] text-accent hover:underline"
                                data-testid={`link-${it.symbol}`}
                              >
                                行动卡 →
                              </Link>
                            </div>
                            {/* 状态写回：按三态给动作。持仓优先于计划，所以 holding 行不给「加计划」；
                                已有计划的明细编辑归行动卡的 PlanEditor，这里只做状态增删，不重造编辑器。
                                操作区收进折叠：明细清单本来就是「看」为主，每只票都挂一排按钮太吵，
                                折叠后仍保留全部动作，测试按文字找按钮也不受影响。 */}
                            <details className="tsp-compact border-t border-border/20 px-3 py-1 text-muted">
                              <summary className="cursor-pointer text-xs text-secondary" data-testid={`ops-summary-${it.symbol}`}>改状态 / 管理这只票</summary>
                              <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs">
                              {it.state === 'watch' && (
                                <button data-testid={`btn-plan-${it.symbol}`} onClick={() => { setActionErr(null); setDialog({ symbol: it.symbol, kind: 'plan' }) }}
                                  className="rounded border border-border/60 px-1.5 py-0.5 text-secondary hover:bg-elevated">
                                  + 加计划
                                </button>
                              )}
                              {it.state === 'watch' && (
                                <button data-testid={`btn-hold-${it.symbol}`} onClick={() => { setActionErr(null); setDialog({ symbol: it.symbol, kind: 'hold' }) }}
                                  className="rounded border border-accent/50 px-1.5 py-0.5 text-accent hover:bg-elevated">
                                  录为持仓
                                </button>
                              )}
                              {it.state === 'planning' && (
                                <button data-testid={`btn-hold-${it.symbol}`} onClick={() => { setActionErr(null); setDialog({ symbol: it.symbol, kind: 'hold' }) }}
                                  className="rounded border border-accent/50 px-1.5 py-0.5 text-accent hover:bg-elevated">
                                  录为持仓
                                </button>
                              )}
                              {it.state === 'planning' && (
                                <button data-testid={`btn-unplan-${it.symbol}`} onClick={() => { setActionErr(null); removePlan.mutate(it.symbol) }}
                                  disabled={removePlan.isPending}
                                  className="rounded border border-border/60 px-1.5 py-0.5 text-muted hover:text-danger disabled:opacity-50">
                                  撤销计划
                                </button>
                              )}
                              {it.state === 'holding' && confirmRemove !== it.symbol && (
                                <button data-testid={`btn-remove-${it.symbol}`} onClick={() => { setActionErr(null); setConfirmRemove(it.symbol) }}
                                  className="rounded border border-border/60 px-1.5 py-0.5 text-muted hover:text-danger">
                                  移除持仓
                                </button>
                              )}
                              {it.state === 'holding' && confirmRemove === it.symbol && (
                                <>
                                  <span className="text-warning">删除后会从盈亏/权重里移除，确认？</span>
                                  <button data-testid={`btn-confirm-remove-${it.symbol}`} onClick={() => removeHolding.mutate(it.symbol)}
                                    disabled={removeHolding.isPending}
                                    className="rounded border border-danger/50 px-1.5 py-0.5 text-danger disabled:opacity-50">
                                    确认移除
                                  </button>
                                  <button onClick={() => setConfirmRemove(null)}
                                    className="rounded border border-border/60 px-1.5 py-0.5 text-secondary">
                                    取消
                                  </button>
                                </>
                              )}
                              </div>
                            </details>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {/* 单票规则检验 —— 进阶功能，整块收起，页面默认先聚焦「我的持仓」。 */}
          <details className="border-t border-border/40 pt-4">
            <summary className="cursor-pointer text-base font-semibold">单票规则检验（进阶，点开）</summary>
            <p className="mt-1 text-xs leading-relaxed text-muted">
              想知道某条经验规则历史上管不管用，再展开这里：填一只代码，看它出现类似走势后之后 N 天的上涨比例，以及系统对规则有效性的诚实评价。只是历史统计，不是买卖建议。
            </p>
            <div className="mt-2 space-y-3">
              <div className="flex gap-2">
                <input value={ruleInput}
                  onChange={e => setRuleInput(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') applyRule() }}
                  placeholder="代码或名称，如 000333.SZ / 美的集团"
                  className="flex-1 rounded-btn border border-border/60 bg-transparent px-3 py-2 text-sm" />
                <button onClick={applyRule}
                  className="rounded-btn bg-accent px-4 py-2 text-sm text-white">检验</button>
              </div>
              <RuleValidationPanel symbol={ruleSymbol} />
              {ruleSymbol && <ReconcilePanel symbol={ruleSymbol} />}
            </div>
          </details>

          {actionErr && (
            <div className="rounded-btn border border-danger/40 bg-danger/5 px-3 py-2 text-[11px] text-danger">
              状态变更失败：{actionErr}
            </div>
          )}
        </div>
      </div>
      {dialog && (
        <StateActionDialog
          symbol={dialog.symbol}
          kind={dialog.kind}
          onClose={() => setDialog(null)}
          onWritten={onWritten}
        />
      )}
    </div>
  )
}
