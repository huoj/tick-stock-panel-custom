import { lazy } from 'react'
import { ShieldCheck } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { IconLots } from '@/components/BrandIcons'

export const ActionCard = lazy(() => import('./Page').then(m => ({ default: m.ActionCard })))
export const PositionOverview = lazy(() => import('./Overview').then(m => ({ default: m.PositionOverview })))
export const PortfolioRiskPage = lazy(() => import('./PortfolioRisk').then(m => ({ default: m.PortfolioRisk })))

/**
 * 行动卡 + 持仓总览 + 组合风控。
 *
 * 核心设计 (用户要求 2026-11-13): 把「事实 / 系统推断 / 用户计划」三段
 * 明显分开, **不提供买入/卖出分数**。
 * 总览页把「自选观察 / 计划买入 / 实际持仓」三态分开, 只有实际持仓计入盈亏。
 * 组合风控页把「真实持仓」与「自选观察」两种口径分开算, 顶部固定放真仓录入面板。
 */
const extension: FrontendExtension = {
  id: 'official.action-card',
  apiVersion: 1,
  routes: [
    { id: 'action-card', path: '/action-card', component: ActionCard },
    { id: 'position-overview', path: '/position-overview', component: PositionOverview },
    { id: 'portfolio-risk', path: '/portfolio-risk', component: PortfolioRiskPage },
  ],
  navigation: [
    { id: 'action-card', routeId: 'action-card', label: '行动卡', icon: IconLots },
    { id: 'position-overview', routeId: 'position-overview', label: '持仓总览', icon: IconLots },
    { id: 'portfolio-risk', routeId: 'portfolio-risk', label: '组合风控', icon: ShieldCheck },
  ],
}

export default extension
