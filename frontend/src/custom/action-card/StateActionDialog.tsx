import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '@/lib/api'

type Kind = 'plan' | 'hold'

interface DialogProps {
  symbol: string
  onClose: () => void
  /** 写成功后由页面统一失效缓存 (分类/持仓/组合风险/行动卡)。 */
  onWritten: (symbol: string) => void
}

const inputCls = 'w-full rounded-btn border border-border/60 bg-transparent px-2 py-1 text-xs'

/**
 * 三态清单的行内「状态写回」。
 *
 * 这里只做快速登记：把 自选观察 变成 计划买入 / 实际持仓。
 * 计划的完整分批明细归行动卡页的 PlanEditor，不在这里重造第二套编辑器；
 * 两边共用同一个 api.positionPlanSave / 后端 validate_plan，写入规则不重复。
 */
export function StateActionDialog({
  symbol,
  kind,
  onClose,
  onWritten,
}: DialogProps & { kind: Kind }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-md rounded-lg border border-border bg-base p-4 space-y-3"
        onClick={e => e.stopPropagation()}>
        {kind === 'plan'
          ? <PlanForm symbol={symbol} onClose={onClose} onWritten={onWritten} />
          : <HoldForm symbol={symbol} onClose={onClose} onWritten={onWritten} />}
      </div>
    </div>
  )
}

function numOrNull(s: string): number | null {
  return s.trim() === '' ? null : Number(s)
}

function FormFooter({
  pending,
  canSave,
  submitLabel,
  onCancel,
  onSubmit,
}: {
  pending: boolean
  canSave: boolean
  submitLabel: string
  onCancel: () => void
  onSubmit: () => void
}) {
  return (
    <div className="flex justify-end gap-2 pt-1">
      <button onClick={onCancel} className="rounded-btn border border-border/60 px-3 py-1 text-xs">取消</button>
      <button onClick={onSubmit} disabled={!canSave || pending}
        className="rounded-btn bg-accent px-3 py-1 text-xs text-white disabled:opacity-50">
        {pending ? '保存中…' : submitLabel}
      </button>
    </div>
  )
}

function PlanForm({ symbol, onClose, onWritten }: DialogProps) {
  const [holdDays, setHoldDays] = useState('')
  const [lossPct, setLossPct] = useState('')
  const [price, setPrice] = useState('')
  const [note, setNote] = useState('')

  const hold = numOrNull(holdDays)
  const loss = numOrNull(lossPct)
  const buyPrice = numOrNull(price)
  // 后端 validate_plan 是 fail-closed: 空值必须传 null, 且不接受非数字。
  // 前端先把 NaN 挡住, 否则 JSON.stringify(NaN) 会静默变成 null, 把用户填的东西丢掉。
  const badNumber = [hold, loss, buyPrice].some(v => v != null && !Number.isFinite(v))
  const filled = !!(holdDays.trim() || lossPct.trim() || price.trim() || note.trim())
  const canSave = filled && !badNumber

  const save = useMutation({
    mutationFn: () => api.positionPlanSave(symbol, {
      symbol,
      planned_hold_days: hold,
      acceptable_loss_pct: loss,
      batches: buyPrice != null && buyPrice > 0
        ? [{ price: buyPrice, amount: 0, label: '', done: false }]
        : [],
      note,
    }),
    onSuccess: () => { onWritten(symbol); onClose() },
  })

  return (
    <div className="space-y-3">
      <div className="text-sm font-medium">登记买入计划 · <span className="font-mono">{symbol}</span></div>
      <div className="text-[10px] leading-4 text-warning">
        保存后它会被记为「计划买入」—— 只是记录，不会产生订单、不计入盈亏。分批明细到「行动卡 →」里编辑。
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label className="space-y-1">
          <span className="text-[10px] text-secondary">计划持有（天，整数）</span>
          <input aria-label="计划持有天数" value={holdDays} onChange={e => setHoldDays(e.target.value)}
            className={inputCls} placeholder="30" />
        </label>
        <label className="space-y-1">
          <span className="text-[10px] text-secondary">可接受亏损（%）</span>
          <input aria-label="可接受亏损百分比" value={lossPct} onChange={e => setLossPct(e.target.value)}
            className={inputCls} placeholder="8" />
        </label>
      </div>
      <label className="block space-y-1">
        <span className="text-[10px] text-secondary">计划买入价（选填，填正数才算一批）</span>
        <input aria-label="计划买入价" value={price} onChange={e => setPrice(e.target.value)}
          className={inputCls} placeholder="留空则先只记一条备注" />
      </label>
      <label className="block space-y-1">
        <span className="text-[10px] text-secondary">备注</span>
        <textarea aria-label="计划备注" value={note} onChange={e => setNote(e.target.value)} rows={2}
          className={inputCls} placeholder="为什么想买、触发条件…" />
      </label>
      {badNumber && <div className="text-[11px] text-danger">数字字段请填有效数字。</div>}
      {save.error && <div className="text-[11px] text-danger">{(save.error as Error).message}</div>}
      <FormFooter pending={save.isPending} canSave={canSave} submitLabel="保存计划"
        onCancel={onClose} onSubmit={() => save.mutate()} />
    </div>
  )
}

function HoldForm({ symbol, onClose, onWritten }: DialogProps) {
  const [qty, setQty] = useState('')
  const [avgCost, setAvgCost] = useState('')
  const [entryDate, setEntryDate] = useState('')

  const q = Number(qty)
  const c = Number(avgCost)
  // 数量必须为正；成本允许为负（反复做 T 把成本摊薄后，券商 App 里成本会显示成负数）。
  // 成本 0 在录入约定里代表"没填"，仍拒绝；NaN（用户只敲了个 "-"）也不提交。
  const canSave = qty.trim() !== '' && avgCost.trim() !== ''
    && Number.isFinite(q) && q > 0
    && Number.isFinite(c) && c !== 0

  const save = useMutation({
    mutationFn: () => api.manualHoldingSave(symbol, {
      qty: q,
      avg_cost: c,
      entry_date: entryDate || null,
    }),
    onSuccess: () => { onWritten(symbol); onClose() },
  })

  return (
    <div className="space-y-3">
      <div className="text-sm font-medium">录入真实持仓 · <span className="font-mono">{symbol}</span></div>
      <div className="text-[10px] leading-4 text-warning">
        保存后它变成「实际持仓」，会计入盈亏与权重。名称/类型由后端按代码补全。
        成本可填负数（反复做 T 摊薄后的正常结果）；此时盈亏金额照算，收益率(%)不适用会显示「—」。
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label className="space-y-1">
          <span className="text-[10px] text-secondary">数量（股/份）</span>
          <input aria-label="持仓数量" value={qty} onChange={e => setQty(e.target.value)}
            className={inputCls} placeholder="1000" />
        </label>
        <label className="space-y-1">
          <span className="text-[10px] text-secondary">平均成本（元）</span>
          <input aria-label="平均成本" value={avgCost} onChange={e => setAvgCost(e.target.value)}
            className={inputCls} placeholder="70.00" />
        </label>
      </div>
      <label className="block space-y-1">
        <span className="text-[10px] text-secondary">建仓日期（选填）</span>
        <input type="date" aria-label="建仓日期" value={entryDate} onChange={e => setEntryDate(e.target.value)}
          className={inputCls} />
      </label>
      {save.error && <div className="text-[11px] text-danger">{(save.error as Error).message}</div>}
      <FormFooter pending={save.isPending} canSave={canSave} submitLabel="保存持仓"
        onCancel={onClose} onSubmit={() => save.mutate()} />
    </div>
  )
}
