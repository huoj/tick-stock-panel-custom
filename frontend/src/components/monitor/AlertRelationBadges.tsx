import { cn } from '@/lib/cn'
import type { AlertEvent } from '@/lib/api'

/** 映射等级的展示口径。
 *
 * `mapping_level` 是采集侧给的「这条新闻跟这只股票有多近」的分级：
 * A/B 算直接映射（同一产业链上下游、同一主体），C/D 只能算线索
 * （仅同行业、或需要多级传导）。**这里不把它翻译成"利好/利空强弱"**，
 * 只说明"有多近" —— 近的事也不一定是好消息，远的事也可能是。
 */
const LEVEL_STYLE: Record<string, { cls: string; label: string }> = {
  A: { cls: 'border-danger/40 bg-danger/10 text-danger', label: '映射 A 直接' },
  B: { cls: 'border-warn/40 bg-warn/10 text-warn', label: '映射 B 较直接' },
  C: { cls: 'border-border bg-elevated text-muted', label: '映射 C 弱' },
  D: { cls: 'border-border bg-elevated text-muted', label: '映射 D 很弱' },
}

const RELATION_STYLE: Record<string, { cls: string; label: string }> = {
  直接: { cls: 'border-accent/40 bg-accent/10 text-accent', label: '关系 直接' },
  间接: { cls: 'border-border bg-elevated text-muted', label: '关系 间接·推断' },
}

const VERIFY_STYLE: Record<string, string> = {
  fact: 'border-accent/40 bg-accent/10 text-accent',
  evidenced: 'border-accent/40 bg-accent/10 text-accent',
  reported: 'border-warn/40 bg-warn/10 text-warn',
  inferred: 'border-border bg-elevated text-muted',
  pending: 'border-border bg-elevated text-muted/70',
}

function pickString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/**
 * 告警的「为什么跟这只股票有关」小条。
 *
 * 原来的告警列表只渲染 severity / symbol / 文案，`mapping_level`、
 * `directness`、`verification`、`impact_path_text` 这些字段虽然在数据里
 * （实测 285 条里 58 条有 mapping_level），但**一个都没显示**，于是
 * 「宏柏新材 + 上海推动大兆瓦海上风机」这种弱关联推测和「宏柏新材:股东减持
 * 公告」在界面上长得一模一样。这个组件把它们摊开，并且对缺失字段如实写
 * 「采集侧未提供」，不留白、不默认成"直接"。
 */
export function AlertRelationBadges({ ev }: { ev: AlertEvent }) {
  const level = pickString(ev.mapping_level).toUpperCase()
  const levelDesc = pickString(ev.mapping_level_desc)
  const directness = pickString(ev.directness)
  const verification = pickString(ev.verification)
  const verificationLabel = pickString(ev.verification_label)
  const mappingBasis = pickString(ev.mapping_basis)
  const impactPath = pickString(ev.impact_path_text)
  const uncertainty = pickString(ev.uncertainty)
  const sourceName = pickString(ev.source_name)
  const sourceUrl = pickString(ev.source_url)
  const publishedAt = pickString(ev.published_at)
  const eventTitle = pickString(ev.event_title)
  const tierLabel = pickString(ev.source_tier_label)

  const hasAny = Boolean(level || directness || verification || impactPath || mappingBasis)
  if (!hasAny) {
    // 完全没有映射信息时不显示空壳，只在政策类来源上明说一句"没有"，
    // 免得用户以为"没显示"等于"关系很近"。
    if (!ev.source?.startsWith('policy')) return null
    return (
      <div className="mt-1 text-[10px] text-muted/60">
        采集侧未提供映射等级与传导路径 —— 无法判断这条跟本标的有多近，请自行核对原文
      </div>
    )
  }

  const levelInfo = LEVEL_STYLE[level]
  const relationInfo = RELATION_STYLE[directness]
  const verifyCls = VERIFY_STYLE[verification] ?? VERIFY_STYLE.pending

  return (
    <div className="mt-1 space-y-1">
      <div className="flex flex-wrap items-center gap-1">
        {levelInfo ? (
          <span
            className={cn('rounded border px-1.5 py-0.5 text-[9px] font-medium', levelInfo.cls)}
            title={levelDesc || undefined}
          >
            {levelInfo.label}
          </span>
        ) : (
          <span className="rounded border border-border bg-elevated px-1.5 py-0.5 text-[9px] text-muted/70">
            映射等级未提供
          </span>
        )}
        {relationInfo ? (
          <span className={cn('rounded border px-1.5 py-0.5 text-[9px] font-medium', relationInfo.cls)}>
            {relationInfo.label}
          </span>
        ) : (
          <span className="rounded border border-border bg-elevated px-1.5 py-0.5 text-[9px] text-muted/70">
            关系未判定
          </span>
        )}
        {verification && (
          <span className={cn('rounded border px-1.5 py-0.5 text-[9px] font-medium', verifyCls)}>
            核实 {verificationLabel || verification}
          </span>
        )}
        {publishedAt && (
          <span className="text-[9px] text-muted/70">发布于 {publishedAt}</span>
        )}
        {sourceName && (
          <span className="text-[9px] text-muted/70">
            来源{' '}
            {sourceUrl ? (
              <a
                href={sourceUrl}
                target="_blank"
                rel="noreferrer noopener"
                className="underline decoration-dotted hover:text-accent"
                onClick={(e) => e.stopPropagation()}
              >
                {sourceName}
              </a>
            ) : (
              sourceName
            )}
            {tierLabel ? `（${tierLabel}）` : ''}
          </span>
        )}
      </div>

      {(eventTitle || impactPath || mappingBasis || uncertainty) && (
        <details className="group/rel">
          <summary className="cursor-pointer list-none text-[10px] text-muted/70 hover:text-foreground">
            <span className="underline decoration-dotted">为什么相关 / 传导路径</span>
          </summary>
          <div className="mt-1 space-y-0.5 border-l border-border/60 pl-2 text-[10px] leading-relaxed text-muted">
            {eventTitle && <div>事件：{eventTitle}</div>}
            {impactPath && <div>传导：{impactPath}</div>}
            {mappingBasis && <div>判定依据：{mappingBasis}</div>}
            {uncertainty && <div className="text-warn/80">不确定处：{uncertainty}</div>}
          </div>
        </details>
      )}
    </div>
  )
}
