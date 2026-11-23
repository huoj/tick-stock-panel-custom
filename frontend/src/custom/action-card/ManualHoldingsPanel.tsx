import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Wallet } from 'lucide-react'
import { api, type ManualHoldingInput } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { Skeleton } from '@/components/data/Skeleton'

function money(v: number | null | undefined): string {
  if (v == null) return '—'
  const abs = Math.abs(v)
  if (abs >= 1e8) return `${(v / 1e8).toFixed(2)} 亿`
  if (abs >= 1e4) return `${(v / 1e4).toFixed(2)} 万`
  return v.toFixed(2)
}

function pct(v: number | null | undefined, digits = 2): string {
  if (v == null) return '—'
  return `${v.toFixed(digits)}%`
}

const EMPTY: ManualHoldingInput = { qty: 0, avg_cost: 0, entry_date: null, note: '' }

/**
 * 手工录入真实持仓 —— P0 缺口：不玩模拟盘、也不接券商接口的人没有真实权重，
 * 于是「组合风险」「行动卡持仓段」全部算不出来。
 *
 * 这里只收三件事：数量、成本、建仓日。**可接受亏损与计划持有期不在这里填** ——
 * 那是用户意图，归「买入计划」管，一处一个来源，避免两处数字打架。
 */
export function ManualHoldingsPanel({ onChanged }: { onChanged?: () => void }) {
  const qc = useQueryClient()
  const [form, setForm] = useState<ManualHoldingInput>(EMPTY)
  const [symbol, setSymbol] = useState('')
  const [cashInput, setCashInput] = useState<string>('')
  // 成本用字符串单独托管: 受控的 type="number" 输入在只敲 "-" 时会被浏览器
  // 吞成空串, 负数根本敲不进来; 改成字符串态 + 文本输入, 负成本才录得进去。
  const [costText, setCostText] = useState('')
  const [err, setErr] = useState<string | null>(null)

  const list = useQuery({
    queryKey: QK.manualHoldings,
    queryFn: () => api.manualHoldings(),
  })

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: QK.manualHoldings })
    // 持仓变了，三态分类与组合风险的口径都会跟着变。
    // 组合风险的 key 前缀是 [...QK.customSignals, 'portfolio-risk']，单独
    // invalidate ['portfolio-risk'] 匹配不到（前缀不同），必须带完整前缀。
    void qc.invalidateQueries({ queryKey: QK.positionPlans })
    void qc.invalidateQueries({ queryKey: [...QK.customSignals, 'portfolio-risk'] })
    void qc.invalidateQueries({ queryKey: ['buy-simulation'] })
    onChanged?.()
  }

  const save = useMutation({
    // 成本从字符串态解析: 让 "-5.2" 这类负成本(做 T 摊薄)原样提交。
    mutationFn: () => api.manualHoldingSave(symbol.trim(), { ...form, avg_cost: Number(costText) }),
    onSuccess: () => {
      setSymbol('')
      setForm(EMPTY)
      setCostText('')
      setErr(null)
      refresh()
    },
    onError: (e: Error) => setErr(e.message),
  })

  const remove = useMutation({
    mutationFn: (sym: string) => api.manualHoldingDelete(sym),
    onSuccess: () => { setErr(null); refresh() },
    onError: (e: Error) => setErr(e.message),
  })

  const setCash = useMutation({
    mutationFn: () => api.manualHoldingsSetCash(cashInput.trim() === '' ? null : Number(cashInput)),
    onSuccess: () => { setErr(null); setCashInput(''); refresh() },
    onError: (e: Error) => setErr(e.message),
  })

  const data = list.data
  const summary = data?.summary
  // 成本允许负数(做 T 摊薄), 0 代表"没填"仍拒绝; NaN(用户只敲了个 "-")也不提交。
  const canSave = symbol.trim().length > 0 && Number(form.qty) > 0
    && costText.trim() !== '' && Number.isFinite(Number(costText)) && Number(costText) !== 0

  return (
    <div className="rounded-btn border border-border/50">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 border-b border-border/40 px-3 py-2.5">
        <div className="flex items-center gap-2">
          <Wallet size={15} className="text-accent" />
          <span className="text-base font-semibold">我的真实持仓</span>
        </div>
        <span className="text-xs text-muted">
          录数量 / 成本 / 建仓日，下面的市值、权重、盈亏，以及组合风控都会按你的真仓算
        </span>
      </div>

      <div className="px-3 py-2 space-y-2">
        {/* 汇总 */}
        {list.isLoading && <Skeleton className="h-16 w-full" />}
        {data && data.count === 0 && (
          <div className="text-sm leading-relaxed text-muted">
            还没有录入。填一只试试 —— 录完之后「按状态看清每一只票」里它会变成「实际持仓」，组合风控里也会出现它的权重。
          </div>
        )}
        {data && data.count > 0 && summary && (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <div>
              <div className="text-xs text-muted">持仓市值</div>
              <div className="font-mono text-base font-medium">{money(summary.market_value)} 元</div>
            </div>
            <div>
              <div className="text-xs text-muted">权益（含现金）</div>
              <div className="font-mono text-base font-medium">{money(summary.total)} 元</div>
            </div>
            <div>
              <div className="text-xs text-muted">浮动盈亏</div>
              <div className={cn('font-mono text-base font-medium',
                (summary.total_pnl ?? 0) > 0 ? 'text-danger'
                  : (summary.total_pnl ?? 0) < 0 ? 'text-accent' : 'text-muted')}>
                {money(summary.total_pnl)} 元
              </div>
            </div>
            <div>
              <div className="text-xs text-muted">估值口径</div>
              <div className={cn('text-sm', summary.price_source === 'cost' ? 'text-warning' : 'text-muted')}>
                {summary.price_source === 'live' ? '盘中快照'
                  : summary.price_source === 'daily' ? '最近日线收盘'
                  : summary.price_source === 'cost' ? '按成本（盈亏不可信）' : '—'}
              </div>
            </div>
          </div>
        )}

        {/* 明细 */}
        {data && data.positions.length > 0 && (
          <div className="overflow-x-auto">
            <table className="tsp-dense-table w-full text-[10px]">
              <thead className="text-muted">
                <tr className="border-b border-border/40">
                  <th className="py-1 text-left font-medium">标的</th>
                  <th className="py-1 text-right font-medium">数量</th>
                  <th className="py-1 text-right font-medium">成本</th>
                  <th className="py-1 text-right font-medium">现价</th>
                  <th className="py-1 text-right font-medium">市值</th>
                  <th className="py-1 text-right font-medium">权重</th>
                  <th className="py-1 text-right font-medium">浮盈亏</th>
                  <th className="py-1 text-right font-medium">持有</th>
                  <th className="py-1"></th>
                </tr>
              </thead>
              <tbody>
                {data.positions.map(p => (
                  <tr key={p.symbol} className="border-b border-border/20 last:border-0">
                    <td className="py-1">
                      <span className="font-mono">{p.symbol}</span>
                      {p.name && <span className="ml-1 text-muted">{p.name}</span>}
                      {!p.priced && <span className="ml-1 text-warning">按成本</span>}
                    </td>
                    <td className="py-1 text-right font-mono">{p.qty}</td>
                    <td className="py-1 text-right font-mono">{p.avg_cost.toFixed(3)}</td>
                    <td className="py-1 text-right font-mono">
                      {p.priced ? p.last_price.toFixed(3) : '—'}
                    </td>
                    <td className="py-1 text-right font-mono">{money(p.market_value)}</td>
                    <td className="py-1 text-right font-mono">{pct((p.weight ?? 0) * 100, 1)}</td>
                    <td className={cn('py-1 text-right font-mono',
                      (p.pnl ?? 0) > 0 ? 'text-danger' : (p.pnl ?? 0) < 0 ? 'text-accent' : 'text-muted')}>
                      {p.pnl == null ? '—' : `${money(p.pnl)}（${pct(p.pnl_pct)}）`}
                    </td>
                    <td className="py-1 text-right text-muted">
                      {p.holding_days == null ? '—' : `${p.holding_days} 天`}
                    </td>
                    <td className="py-1 text-right">
                      <button title="删除这条记录" aria-label={`删除 ${p.symbol}`}
                        onClick={() => remove.mutate(p.symbol)}
                        className="rounded px-1 text-muted hover:text-danger">
                        <Trash2 size={11} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* 录入表单 —— 次要操作收进折叠，别让 6 个小输入框一进页面就占满视线。 */}
        <details className="border-t border-border/40 pt-2">
          <summary className="cursor-pointer text-sm font-medium text-secondary">
            ＋ 录入 / 修改一条持仓
          </summary>
          <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-6">
            <input value={symbol} onChange={e => setSymbol(e.target.value)}
              placeholder="代码或名称"
              aria-label="标的代码或名称"
              className="col-span-2 rounded-btn border border-border/60 bg-transparent px-2.5 py-1.5 text-sm" />
            <input type="number" min={0} step={100} value={form.qty || ''}
              onChange={e => setForm({ ...form, qty: Number(e.target.value) })}
              placeholder="数量"
              aria-label="持仓数量"
              className="rounded-btn border border-border/60 bg-transparent px-2.5 py-1.5 text-sm" />
            <input type="text" inputMode="decimal" value={costText}
              onChange={e => setCostText(e.target.value)}
              placeholder="成本价（可为负）"
              aria-label="平均成本价"
              className="rounded-btn border border-border/60 bg-transparent px-2.5 py-1.5 text-sm" />
            <input type="date" value={form.entry_date ?? ''}
              onChange={e => setForm({ ...form, entry_date: e.target.value || null })}
              aria-label="建仓日期"
              className="rounded-btn border border-border/60 bg-transparent px-2.5 py-1.5 text-sm" />
            <button disabled={!canSave || save.isPending} onClick={() => save.mutate()}
              className={cn('flex items-center justify-center gap-1 rounded-btn px-2.5 py-1.5 text-sm text-white',
                canSave && !save.isPending ? 'bg-accent' : 'cursor-not-allowed bg-muted/40')}>
              <Plus size={13} />
              {save.isPending ? '保存中…' : '录入'}
            </button>
          </div>
        </details>

        {/* 现金 —— 录入动作次要，收进折叠；说明保留在摘要里，一眼能看见。 */}
        <details className="border-t border-border/40 pt-2">
          <summary className="cursor-pointer text-sm font-medium text-secondary">
            ＋ 设置账户现金（当前 {data?.cash == null ? '未录入' : `${money(data.cash)} 元`}）
          </summary>
          <p className="mt-1 text-xs leading-relaxed text-muted">
            权重分母 = 现金 + 持仓市值；不填现金时权重合计会等于 1，看起来像满仓。
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <input type="number" min={0} step={1000} value={cashInput}
              onChange={e => setCashInput(e.target.value)}
              placeholder={data?.cash == null ? '未录入' : String(data.cash)}
              aria-label="账户现金余额"
              className="w-32 rounded-btn border border-border/60 bg-transparent px-2.5 py-1.5 text-sm" />
            <button disabled={setCash.isPending} onClick={() => setCash.mutate()}
              className="rounded-btn border border-border/60 px-3 py-1.5 text-sm hover:bg-elevated">
              {setCash.isPending ? '保存中…' : '保存现金'}
            </button>
          </div>
        </details>

        {err && (
          <div className="rounded-btn border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
            {err}
          </div>
        )}

        {data && data.limitations.length > 0 && (
          <details className="text-xs leading-relaxed text-muted">
            <summary className="cursor-pointer">口径说明（{data.limitations.length} 条）—— 哪些数没跟券商对账、哪些是估算</summary>
            <ul className="mt-1 list-disc space-y-1 pl-5">
              {data.limitations.map((t, i) => <li key={i}>{t}</li>)}
            </ul>
          </details>
        )}
      </div>
    </div>
  )
}
