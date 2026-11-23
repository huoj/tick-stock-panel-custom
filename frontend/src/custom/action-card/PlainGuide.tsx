import { useState } from 'react'
import { BookOpen, ChevronDown } from 'lucide-react'
import { cn } from '@/lib/cn'

export interface PlainTerm {
  term: string
  explain: string
}

/**
 * 面向普通投资者的「白话引导 + 名词表」，行动卡 / 组合风控两页共用这一个
 * 渲染器，具体内容各页自己给。
 *
 * 为什么不直接改指标标签：组合风控的主体在共享组件 PortfolioRiskPanel 里，
 * 模拟盘页也在用，标签文案还被 RiskPanels.test.tsx 断言。把解释做成页面级
 * 增量，既能让这两页的人看懂，又不波及模拟盘、不动既有测试。
 */
/** 前几个最常用的词直接摊开，不用点开就能看懂；其余收进折叠区。 */
const PREVIEW_COUNT = 3

function TermRows({ terms }: { terms: PlainTerm[] }) {
  return (
    <dl className="space-y-1.5">
      {terms.map(t => (
        <div key={t.term} className="text-xs leading-relaxed">
          <dt className="inline font-medium text-foreground">{t.term}：</dt>
          <dd className="inline text-secondary">{t.explain}</dd>
        </div>
      ))}
    </dl>
  )
}

export function PlainGuide({ intro, terms }: { intro: string; terms: PlainTerm[] }) {
  const [open, setOpen] = useState(false)
  const preview = terms.slice(0, PREVIEW_COUNT)
  const rest = terms.slice(PREVIEW_COUNT)
  return (
    <section className="rounded-btn border border-accent/25 bg-accent/5 px-3 py-2.5">
      <div className="flex items-start gap-2">
        <BookOpen className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
        <p className="text-sm leading-relaxed text-foreground">{intro}</p>
      </div>
      {/* 常用词直接摊开：普通投资者不用点开也能看懂个大概 */}
      <div className="mt-2 border-t border-border/40 pt-2">
        <TermRows terms={preview} />
      </div>
      {rest.length > 0 && (
        <>
          <button
            type="button"
            onClick={() => setOpen(v => !v)}
            aria-expanded={open}
            className="mt-1.5 inline-flex items-center gap-1 text-xs text-accent hover:underline"
          >
            <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', open && 'rotate-180')} />
            {open ? '收起其余名词' : `其余 ${rest.length} 个名词的白话解释，点开看`}
          </button>
          {open && (
            <div className="mt-2 border-t border-border/40 pt-2">
              <TermRows terms={rest} />
            </div>
          )}
        </>
      )}
    </section>
  )
}

/** 行动卡里出现的专业词，用大白话逐条翻译。 */
export const ACTION_CARD_TERMS: PlainTerm[] = [
  { term: '收盘 / 涨跌', explain: '收盘是当天最后一笔成交价；涨跌是它比前一天收盘高了或低了多少。' },
  { term: '量比(20日)', explain: '今天的成交量跟过去 20 天的平均比放大了多少，明显超过 100% 表示今天交易突然很活跃。' },
  { term: '收盘位置', explain: '收盘价落在当天「最低～最高」这区间里的位置：100% 是收在最高附近，0% 是收在最低附近。' },
  { term: 'RSI14', explain: '用最近 14 天的涨跌衡量「涨得急不急」，取值 0～100。偏高常被看作短期追高、偏低常被看作短期超跌，只是参考，不是买卖信号。' },
  { term: 'ATR14', explain: '最近 14 天平均每天上下波动多少钱，括号里是它占股价的百分比；数字越大说明这票越「上蹿下跳」。' },
  { term: 'MA5 / MA20 / MA60', explain: '过去 5 / 20 / 60 天的平均收盘价，俗称均线，用来看现价是在均线之上还是之下。' },
  { term: '浮盈亏 / 占比 / 持有天数', explain: '按现价算账面上赚或亏多少钱；这只票占你账户多大比例；从买入到现在多少天。' },
  { term: '方向判定 / 看多·看空', explain: '系统汇总的偏多、偏空线索各有几条，只是线索清点，不等于结论。' },
  { term: '之后 N 日上涨概率', explain: '历史上出现类似走势后，N 天里上涨的比例；「基准」是不看形态时的平均上涨比例，「超出 Xpp」是两者相差多少个百分点。只是历史统计，不是预测。' },
  { term: '平均涨跌 / 对基准', explain: '「平均涨跌」是历史上这些相似走势之后 N 天平均涨了或跌了多少——胜率高但平均涨跌是负的，说明赚的时候赚一点、亏的时候亏更多，别只看胜率。「对基准」是跟同期普通情况的差距，负的表示这个形态反而不如不看形态。' },
  { term: '一年分位 / 一年区间', explain: '「一年分位」是现价在过去一年最低到最高这段range里的位置：100% 表示正好在一年最高点，0% 表示在一年最低点。位置越高，追高风险越大。「一年区间」就是那个最低到最高。' },
  { term: '价格地图', explain: '把各种关键价位按价格高低排成一列：现价上面的（红/黄）是历史上的压力位，涨到那里常有人卖；下面的（绿）是支撑位，跌到那里常有人买。失效条件是你该认错的位置，考虑买入是你打算进场的位置。百分比表示离现价多远。' },
  { term: '置信度 / 样本', explain: '这个统计用了多少历史例子、可信程度如何；样本越少，越不能当真。' },
  { term: 'pp（百分点）', explain: '百分比相减的单位：从 40% 到 45% 是 +5pp，不是涨了 5%。' },
  { term: '规则有效性', explain: '系统诚实标注某条经验规则在历史上到底管不管用：有效 / 弱有效 / 反向 / 无预测力 / 样本不足。' },
  { term: '计划持有期 / 可接受亏损 / 分批计划', explain: '你打算拿多少天、最多能忍多少亏损、准备分几次在什么价位买。这些只是记录，不会自动下单，也不计入盈亏。' },
  { term: '计划 vs 实际成交', explain: '把你写下的计划价位和真实成交价对一对，看有没有按计划执行。' },
]

/**
 * 持仓总览里出现的专业词。这一页是「先看清自己手里有什么、哪些算数」，
 * 所以词表围绕三态、成本、市值、权重、口径这几件事解释，不掺买卖建议。
 */
export const POSITION_OVERVIEW_TERMS: PlainTerm[] = [
  { term: '三态：自选观察 / 计划买入 / 实际持仓', explain: '同一只票只归一类，且「实际持仓」最优先。只有真正买进来的才算数、才进盈亏；自选和计划都只是记着，不影响账户。' },
  { term: '我的真实持仓（手工录入）', explain: '不接券商、也不玩模拟盘的人，自己填数量、成本、建仓日，让系统能按你的真仓算市值和权重。填多少就按多少算，系统不替你补手续费或分红。' },
  { term: '数量 / 平均成本 / 建仓日', explain: '买了多少股、每股大概花了多少钱、哪天开始拿的。成本可以填负数——反复做 T 把成本摊薄后，券商 App 里就是这么显示的。' },
  { term: '市值 / 权益 / 现金', explain: '市值 = 现价 × 数量；权益 = 现金 + 所有持仓市值，是算权重的分母；现金是账户里还没买成股票的钱，也是下跌时的缓冲。' },
  { term: '浮动盈亏 / 浮盈亏%', explain: '按现价算，账面上比成本赚了或亏了多少钱、以及赚亏了百分之几。没卖出之前都只是「浮动」，不是落袋的钱。' },
  { term: '权重', explain: '这一只票的市值占你整个账户（含现金）的比例。权重越高，它对账户涨跌的影响越大。' },
  { term: '估值口径', explain: '现价是从哪来的：「盘中快照」是当天实时价，「最近日线收盘」是最新一个交易日的收盘价，「按成本」表示没取到行情、暂时拿成本当价，这时盈亏不可信。' },
  { term: '持有天数', explain: '从建仓日算到现在拿了多久，用来配合「计划持有期」看这笔是不是拿超了或拿短了。' },
  { term: '计划资金校验', explain: '把所有「打算买」的批次金额加总，跟账户可用现金比一比，看看钱够不够全买；已执行 / 待执行分别是已经买了多少、还打算买多少。' },
  { term: '可用现金 / 缺口', explain: '账户里还能动用的钱；如果计划金额超过它，差额就是缺口，全触发时会有部分批次没钱成交。' },
  { term: '计盈亏 / 不计盈亏', explain: '清单里每只票的标记：只有「实际持仓」写「计盈亏」，会进市值和盈亏计算；自选、计划写「不计盈亏」，纯观察。' },
  { term: '状态变更（加计划 / 录为持仓 / 撤销 / 移除）', explain: '就在清单里把一只票从「观察」改成「计划」或「真仓」，或者撤回来。改完系统会重算分类和组合风险，不用跳到别的页面。' },
  { term: '单票规则检验 / 历史有效性', explain: '拿一条经验规则去翻历史：出现这种走势后，之后 N 天到底涨得多不多。系统会诚实标出「有效 / 反向 / 无预测力 / 样本不足」，只是统计，不是荐股。' },
  { term: '计划 vs 实际成交', explain: '把你原先写下的计划买入价，和后来真实成交的价对一对，看是不是按计划执行的、差了多少。' },
  { term: '行动卡', explain: '点进去看某一只票的完整事实、系统推测和你自己的计划，是这页清单里每只票的详情页。' },
]

/** 组合风控里出现的专业词，用大白话逐条翻译。 */
export const PORTFOLIO_RISK_TERMS: PlainTerm[] = [
  { term: '权重', explain: '某只票（或某个行业）占你整个账户的比例。' },
  { term: '集中度 / 行业集中度 / HHI', explain: '「集中度」是钱压在少数几只票或几个行业上的程度；HHI 是它的量化分数，越高越集中；「有效行业数」指虽然买了很多只，按分散程度其实只相当于买了几个不同行业。' },
  { term: '相关性 / 平均相关', explain: '几只票是不是经常同涨同跌，越接近 1 越像同一笔买卖，分散作用越小。' },
  { term: '有效独立注数', explain: '名义上买了 N 只票，扣掉「其实同涨同跌」之后，真正互不相干、能分散风险的赌注有几个。越接近 1，说明看着分散、其实押的是同一件事。' },
  { term: '主题重复（概念）暴露', explain: '几只票其实都靠同一个题材（比如「机器人」）在涨，题材一冷会一起回落。' },
  { term: '年化波动 / 潜在回撤', explain: '这票平时一年里上下晃多大幅度；以及按历史估计，极端情况下可能从高点回落多少。' },
  { term: '风险贡献', explain: '组合整体的风险里，这一只占了多大一份「锅」。' },
  { term: '压力回撤 / 回撤预算', explain: '假设所有持仓同时跌到各自最坏位置，账户最多可能亏多少；「预算」是你给自己设的「最多能忍亏多少」的线，超了就该考虑减仓。这是压力测试的上界，不是预测，也不保证不亏。' },
  { term: '跌 10% / 20% 会怎样', explain: '假如持仓整体跌 10% 或 20%，你的账户分别会少多少百分点(pp)和多少钱。' },
  { term: 'pp（百分点）', explain: '百分比相减的单位，例如 -6.00pp 指账户净值少了 6 个百分点。' },
  { term: '现金比例 / 已投资', explain: '账户里还没买成股票的钱占多少（下跌时的缓冲、也是加仓能力）；「已投资」是已经买成股票的比例。' },
  { term: '账户最大回撤 / 当前距峰值', explain: '账户历史上从最高点最多回落过多少；现在离历史最高点还差多少。' },
  { term: '等权假设', explain: '观察口径里把每只自选都当成「买一样多」来粗算，方便看集中度，不代表你真会这么买。' },
  { term: '买入前仓位模拟', explain: '先算一下「再买这一笔之后，集中度和回撤会不会变差」，再决定要不要下手。' },
]
