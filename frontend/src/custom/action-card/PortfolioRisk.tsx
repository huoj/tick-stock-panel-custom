import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertTriangle, Star } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { Skeleton } from '@/components/data/Skeleton'
import { PortfolioRiskPanel, BuySimulationCard } from '@/components/signals/PortfolioRiskPanel'
import { ManualHoldingsPanel } from './ManualHoldingsPanel'
import { PlainGuide, PORTFOLIO_RISK_TERMS } from './PlainGuide'

/** 相关性矩阵是 O(n²)，观察口径一次别塞太多；超出如实截断。 */
const MAX_OBSERVATION_SYMBOLS = 100

/**
 * 组合风控（独立页）。
 *
 * 之前组合风险只有「模拟盘」页里一个嵌块，走模拟盘账户口径 —— 不玩模拟盘、
 * 用「我的真实持仓」录入的人既找不到页面，也算不出自己的组合。这里把两种
 * 口径摊在同一页、上下分开：
 *
 * 1. **真实持仓口径**：`manual` 走 `GET /api/risk/portfolio?source=manual`，
 *    权重分母 = 现金 + 持仓市值，不归一化；只有这一块计入盈亏与权重。
 * 2. **自选观察口径**：导入自选后按等权假设测算，后端标
 *    `basis=caller_supplied_weights` / `watchlist_observation` 并附提醒；
 *    界面必须显著说明「自选 ≠ 持仓」。
 *
 * 页面顶部固定放「我的真实持仓」录入面板 —— 它是口径 1 的唯一数据来源。
 */
export function PortfolioRisk() {
  const [watchSymbols, setWatchSymbols] = useState<string[]>([])
  const [truncated, setTruncated] = useState(0)

  const holdings = useQuery({
    queryKey: QK.manualHoldings,
    queryFn: () => api.manualHoldings(),
  })
  const hasManual = (holdings.data?.count ?? 0) > 0
  // 这份明细（名称/现价/市值/盈亏/成本/持有天数）此前只用来判断 count > 0 就被丢掉了。
  // 面板的「持仓速览」靠它才能显示「美的集团 91.69 3.67万 +5.00万」而不是一个光秃秃的代码。
  const manualPositions = holdings.data?.positions

  const importWatchlist = useMutation({
    mutationFn: () => api.watchlistList(),
    onSuccess: (data) => {
      const list = (data.symbols ?? []).map(e => e.symbol).filter(Boolean)
      const unique = Array.from(new Set(list))
      const capped = unique.slice(0, MAX_OBSERVATION_SYMBOLS)
      setTruncated(unique.length - capped.length)
      setWatchSymbols(capped)
    },
  })

  // 等权假设：每只 1/n。后端 build_portfolio_report 会把权重归一到合计 1，
  // 这里给小数比例（不是百分数），与 PortfolioRiskPanel 既有入参口径一致。
  const observationHoldings = useMemo(
    () => watchSymbols.map(symbol => ({ symbol, weight: 1 / Math.max(watchSymbols.length, 1) })),
    [watchSymbols],
  )

  return (
    <div className="tsp-readable flex h-full flex-col">
      <PageHeader
        title="组合风控"
        subtitle="帮你看看手里的钱是不是太集中、真跌下来大概会亏多少 · 真实持仓 / 自选观察分开算 · 只有真实持仓计入盈亏与权重"
      />
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <div className="mx-auto max-w-4xl space-y-4">

          <PlainGuide
            intro="这一页是帮你做「体检」，不是荐股：看看钱是不是太压在少数几只票 / 同一个行业上，万一行情整体下跌，账户大概会亏多少，再买一笔会不会让情况更糟。上半部分按你真实录入的持仓算，是正经结论；下半部分只是把自选清单拿来「假如都买了」做个演示，不算你的真实盈亏。看不懂的指标，点开下面的名词解释都有大白话。"
            terms={PORTFOLIO_RISK_TERMS}
          />

          {/* 页面顶部：真实持仓录入 —— 口径 1 的唯一数据来源 */}
          <ManualHoldingsPanel onChanged={() => { void holdings.refetch() }} />

          {/* 口径一：真实持仓 */}
          <section className="space-y-2">
            <div className="text-xs font-medium">
              一、按你真实录入的持仓算（正经结论，计入盈亏）
              <div className="mt-0.5 text-[10px] font-normal leading-relaxed text-muted">
                每只票占账户多少比例，按「这只票的市值 ÷（现金 + 所有持仓市值）」算，含你的买入成本与持有天数
              </div>
            </div>
            {holdings.isLoading && <Skeleton className="h-20 w-full" />}
            {!holdings.isLoading && !hasManual && (
              <div className="rounded-btn border border-border/50 px-3 py-3 text-[11px] leading-5 text-muted">
                还没有录入真实持仓。在上方「我的真实持仓」填一只（数量 / 成本 / 建仓日）后，
                这里会自动帮你算：钱是不是太集中、这些票会不会同涨同跌、真跌下来大概亏多少。
              </div>
            )}
            {hasManual && (
              <>
                <PortfolioRiskPanel manual manualPositions={manualPositions} autoRun />
                <BuySimulationCard manual />
              </>
            )}
          </section>

          {/* 口径二：自选观察 */}
          <section className="space-y-2 border-t border-border/40 pt-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-medium">二、把自选拿来「假如都买了」看看（仅演示，不算盈亏）</span>
              <span className="text-[10px] text-muted">
                假设每只买一样多，看这样会不会太集中；这不是你真实的持仓
              </span>
              <button
                onClick={() => importWatchlist.mutate()}
                disabled={importWatchlist.isPending}
                title="把自选清单带进来做一次等权观察；自选不等于持仓，不计入盈亏"
                className="flex items-center gap-1 rounded-btn border border-border/60 px-2 py-1 text-[11px] text-secondary hover:bg-elevated disabled:opacity-50"
              >
                <Star size={11} />
                {importWatchlist.isPending ? '导入中…' : '导入自选'}
              </button>
              {watchSymbols.length > 0 && (
                <button
                  onClick={() => { setWatchSymbols([]); setTruncated(0) }}
                  className="rounded-btn px-2 py-1 text-[11px] text-muted underline hover:text-secondary"
                >
                  清空
                </button>
              )}
            </div>

            {importWatchlist.isError && (
              <div className="rounded-btn border border-danger/40 bg-danger/5 px-3 py-2 text-[11px] text-danger">
                自选读取失败：{(importWatchlist.error as Error).message}
              </div>
            )}

            {watchSymbols.length > 0 ? (
              <>
                <p className="flex items-start gap-1.5 rounded-btn border border-warning/30 bg-warning/[0.06] px-2.5 py-1.5 text-[11px] leading-relaxed text-warning">
                  <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                  <span>
                    已带入 {watchSymbols.length} 只自选，按等权假设测算。自选 ≠ 持仓：没有成本、
                    没有真实仓位，集中度与回撤预算只有落在上方「我的真实持仓」上才有意义。
                    {truncated > 0 && (
                      <span className="ml-1">
                        （一次最多 {MAX_OBSERVATION_SYMBOLS} 只，另有 {truncated} 只未带入）
                      </span>
                    )}
                  </span>
                </p>
                <PortfolioRiskPanel holdings={observationHoldings} warnObservation autoRun />
              </>
            ) : (
              <div className="rounded-btn border border-border/50 px-3 py-3 text-[11px] leading-5 text-muted">
                点「导入自选」把自选清单带进来，看一次「假如都买」的集中度与相关性。
                这里不会写入真实持仓，也不计入盈亏。
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  )
}
