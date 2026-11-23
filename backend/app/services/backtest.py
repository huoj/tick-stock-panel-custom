"""回测服务(§6.7)。

包 vectorbt — 全项目唯一一处出现 pandas。
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

import numpy as np
import pandas as pd
import polars as pl

from app import fees as fee_rules
from app.config import settings
from app.parquet import scan_enriched_parquet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

# 旧信号回测的指标 warmup 日历窗口 (#201): 与 backtest.factor.FACTOR_WARMUP_DAYS
# 同源 (120 交易日 → 保守取日历日), 覆盖 MA60/MACD/BOLL 等最长回看
_WARMUP_CALENDAR_DAYS = 120 * 1.6

# vectorbt 是 optional extras(见 pyproject.toml).未装时只有 backtest 不可用,其他功能正常.
_vbt = None
_vbt_unavailable_reason: str | None = None


class VectorbtUnavailable(RuntimeError):
    """vectorbt 未安装 — 提示用户 `uv sync --extra backtest`."""


def _get_vbt():
    global _vbt, _vbt_unavailable_reason
    if _vbt is not None:
        return _vbt
    if _vbt_unavailable_reason is not None:
        raise VectorbtUnavailable(_vbt_unavailable_reason)
    try:
        import vectorbt as vbt
        _vbt = vbt
        return _vbt
    except ImportError as e:
        _vbt_unavailable_reason = (
            "vectorbt 未安装 — 它是回测的可选依赖.macOS Intel 用户先 `brew install cmake` "
            "然后 `uv sync --extra backtest`"
        )
        logger.warning("vectorbt unavailable: %s", e)
        raise VectorbtUnavailable(_vbt_unavailable_reason) from e


def is_available() -> bool:
    """供 API 层快速检测."""
    try:
        _get_vbt()
        return True
    except VectorbtUnavailable:
        return False


SignalKind = Literal[
    "macd_golden", "macd_dead",
    "ma_golden_5_20", "ma_dead_5_20",
    "ma_golden_20_60",
    "ma20_breakout", "ma20_breakdown",
    "n_day_high", "n_day_low",
    "boll_breakout_upper", "boll_breakdown_lower",
    "volume_surge",
    "rsi_oversold", "rsi_overbought",
    "stop_loss", "trailing_stop", "max_hold",
]


@dataclass
class BacktestConfig:
    symbols: list[str]
    start: date
    end: date
    # 买入信号(任一触发即买)
    entries: list[str] = field(default_factory=list)
    # 卖出信号(任一触发即卖)
    exits: list[str] = field(default_factory=list)
    # 其他参数
    stop_loss_pct: float | None = None       # 例 -0.05 = -5%
    max_hold_days: int | None = None
    # 成本口径 (单一事实源 app/fees.py): 默认与模拟盘一致 —— 佣金万2.5、
    # 印花税按品种 (股票卖出 0.05% / ETF 免税)、过户费仅股票、单笔最低佣金 5 元。
    # commission_pct 优先于 fees_pct; stamp_tax_pct 显式传 0.0 = 确实零印花税。
    fees_pct: float | None = None
    commission_pct: float | None = None
    stamp_tax_pct: float | None = None
    transfer_fee_pct: float | None = None
    min_commission: float | None = None
    slippage_bps: float = fee_rules.DEFAULT_SLIPPAGE_BPS
    # 撮合
    matching: Literal["close_t", "open_t+1"] = "close_t"
    rsi_oversold_threshold: float = 30
    rsi_overbought_threshold: float = 70
    asset_type: str = "stock"
    # 说明: 本接口走 vectorbt 等权信号回测 (无仓位分配决策), 因此不提供
    # 容量约束 max_participation_pct —— 它只在账户级组合撮合
    # (engine.simulate_portfolio / _simulate_portfolio_matrix) 里有意义。


@dataclass
class BacktestResult:
    run_id: str
    config: dict
    stats: dict
    equity_curve: list[dict]      # [{date, value}]
    trades: list[dict]            # [{symbol, entry_date, exit_date, pnl_pct, ...}]
    per_symbol_stats: list[dict]  # 每只股票的统计


# enriched 表里的信号列名映射 (内置别名 → 列名)。
# 注意: 这不是「支持的信号全集」—— 自定义信号 (data/user_data/custom_signals/
# *.json) 通过 _resolve_signal_column 按 signal_<id> 通用解析, 不在此登记。
_SIGNAL_COLS: dict[SignalKind, str] = {
    "macd_golden": "signal_macd_golden",
    "macd_dead": "signal_macd_dead",
    "ma_golden_5_20": "signal_ma_golden_5_20",
    "ma_dead_5_20": "signal_ma_dead_5_20",
    "ma_golden_20_60": "signal_ma_golden_20_60",
    "ma20_breakout": "signal_ma20_breakout",
    "ma20_breakdown": "signal_ma20_breakdown",
    "n_day_high": "signal_n_day_high",
    "n_day_low": "signal_n_day_low",
    "boll_breakout_upper": "signal_boll_breakout_upper",
    "boll_breakdown_lower": "signal_boll_breakdown_lower",
    "volume_surge": "signal_volume_surge",
}

# 这些名字由 vectorbt 参数 (stop_loss_pct / trailing_* / max_hold_days) 处理,
# 不是布尔信号列, 不应出现在信号矩阵里, 也不该被当成「未知信号」报错。
_NON_MATRIX_SIGNALS: frozenset[str] = frozenset({
    "stop_loss", "take_profit", "trailing_stop", "trailing_take_profit",
    "max_hold", "max_hold_days",
})


def _build_max_hold_exits(entries: pd.DataFrame, max_hold_days: int) -> pd.DataFrame:
    """为每个入场信号在 max_hold_days 个交易日后生成一个强制退出信号。

    返回与 entries 同形状的布尔矩阵, 仅在「入场位之后第 max_hold_days 个交易日」置
    True(不含入场位本身), 供调用方与用户 exits 做 OR。

    两处易错点(见 #198):
    - 必须从全 False 起步。若用 `entries.copy()` 起步会把入场位当成退出位,
      导致入场当日即被强制平仓。
    - 用单步定位写入 `iloc[row, col_loc]`。链式 `iloc[row][col] = True` 写入的是
      临时行副本, 在 pandas Copy-on-Write 语义下不会落到原矩阵(pandas 3.x 直接报错),
      强制退出信号会静默丢失。
    """
    out = pd.DataFrame(False, index=entries.index, columns=entries.columns)
    n = len(entries)
    for col in entries.columns:
        col_loc = out.columns.get_loc(col)
        entry_rows = np.where(entries[col].to_numpy())[0]
        for i in entry_rows:
            end_i = min(int(i) + max_hold_days, n - 1)
            if end_i > i:
                out.iloc[end_i, col_loc] = True
    return out


class BacktestService:
    def __init__(self, repo: KlineRepository) -> None:
        self.repo = repo

    def _load_panel(
        self,
        symbols: list[str],
        start: date,
        end: date,
        asset_type: str = "stock",
    ) -> pd.DataFrame:
        """加载 [date × symbol] 价格面板 — Polars scan_parquet + 即时计算指标。

        **全项目唯一从 Polars 转 pandas 的边界**(§7.4 / ADR-19)。
        asset_type='etf' 时读 ETF enriched。
        """
        try:
            from app.tickflow.repository import enriched_dirname
            enriched_glob = str(self.repo.store.data_dir / enriched_dirname(asset_type) / "**" / "*.parquet")
            # 指标 warmup (#201): MA/MACD/RSI/BOLL 需要区间前的历史窗口,
            # 直接按 [start,end] 过滤后 compute_all 会让区间头部的指标失真。
            # 与挖掘侧同款公式 (mining_runtime: warmup = max(120, bars*1.6)),
            # 此处指标最长回看约 120 交易日, 取保守日历日窗口; 数据不足时
            # 自然退化 (有多少算多少)。计算完成后裁回 [start,end]。
            warmup_start = start - timedelta(days=_WARMUP_CALENDAR_DAYS)
            df = (
                scan_enriched_parquet(enriched_glob)
                .filter(
                    (pl.col("symbol").is_in(symbols))
                    & (pl.col("date") >= warmup_start)
                    & (pl.col("date") <= end)
                )
                .sort(["date", "symbol"])
                .collect()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("backtest load failed: %s", e)
            return pd.DataFrame()

        if df.is_empty():
            return pd.DataFrame()

        # 即时计算指标 + 信号
        # 传入 instruments: 涨跌停类信号 (signal_limit_up / broken_limit_up /
        # limit_down_recovery) 的条件引用 limit_up_price / limit_down_price,
        # 不传则 compute_limit_signals 整段跳过, 这些信号列根本不会产出。
        from app.indicators.pipeline import compute_all
        instruments = None
        try:
            instruments = self.repo.get_instruments_asset(asset_type)
        except Exception as e:  # noqa: BLE001 — 缺维表时降级: 涨跌停族信号不可用
            logger.warning("backtest instruments unavailable (limit signals disabled): %s", e)
        df = compute_all(df, instruments=instruments)
        df = df.filter(pl.col("date") >= start)

        # 选择需要的列。
        # 信号列不能写死白名单: compute_all 已算出全部自定义信号 (含用户新建的
        # data/user_data/custom_signals/*.json 定义), 写死会把它们整列丢掉,
        # 导致 _build_signal_matrix 找不到列而静默返回「no buy signals」。
        # 这里保留所有 signal_/csg_ 前缀列 + 执行所需的行情/指标列。
        base_cols = {
            "date", "symbol", "open", "high", "low", "close", "volume",
            "rsi_14",
        }
        existing = [
            c for c in df.columns
            if c in base_cols or c.startswith(("signal_", "csg_"))
        ]
        df = df.select(existing)

        # to_pandas 边界
        return df.to_pandas(use_pyarrow_extension_array=False)

    @staticmethod
    def _resolve_signal_column(panel: pd.DataFrame, kind: str) -> str | None:
        """把信号名解析成面板里的布尔列名, 解析不到返回 None。

        三种写法都要支持:
        - 列名全称 (signal_volume_surge / csg_xxx)
        - 内置别名 (volume_surge) —— 走 _SIGNAL_COLS 映射
        - 自定义信号 id (如 vol_ma20_break) —— compute_all 按
          data/user_data/custom_signals/*.json 定义算出, 列名统一 signal_<id>
        """
        if kind in _SIGNAL_COLS:
            col = _SIGNAL_COLS[kind]
            return col if col in panel.columns else None
        if kind.startswith(("signal_", "csg_")):
            return kind if kind in panel.columns else None
        candidate = f"signal_{kind}"
        return candidate if candidate in panel.columns else None

    def _build_signal_matrix(
        self,
        panel: pd.DataFrame,
        kinds: list[str],
        config: BacktestConfig,
    ) -> pd.DataFrame:
        """从面板构造 [date × symbol] 的布尔信号矩阵。"""
        if not kinds or panel.empty:
            return pd.DataFrame()

        # pivot 成 [date × symbol] 形式
        result = None
        unknown: list[str] = []
        for kind in kinds:
            mat = None
            if kind == "rsi_oversold":
                mat = (panel.pivot(index="date", columns="symbol", values="rsi_14")
                       < config.rsi_oversold_threshold)
            elif kind == "rsi_overbought":
                mat = (panel.pivot(index="date", columns="symbol", values="rsi_14")
                       > config.rsi_overbought_threshold)
            elif kind in _NON_MATRIX_SIGNALS:
                # stop_loss / trailing / max_hold 通过 vectorbt 参数处理,
                # 不参与信号矩阵
                continue
            else:
                col = self._resolve_signal_column(panel, kind)
                if col is None:
                    unknown.append(kind)
                else:
                    mat = panel.pivot(
                        index="date", columns="symbol", values=col,
                    ).fillna(False).astype(bool)

            if mat is not None:
                result = mat if result is None else (result | mat)

        # 解析不到的信号必须显式报错: 静默忽略会让后端返回「no buy signals」,
        # 用户误以为「这个信号没触发」, 而实际是名字写错或信号未注册。
        if unknown:
            available = sorted(
                c for c in panel.columns if c.startswith(("signal_", "csg_"))
            )
            raise ValueError(
                f"回测无法识别的信号: {unknown}。"
                f"面板可用的信号列: {available[:40]}"
            )

        return result if result is not None else pd.DataFrame()

    def run(self, config: BacktestConfig) -> BacktestResult:
        from app.services.heavy_job_limiter import shared_heavy_job_limiter

        with shared_heavy_job_limiter.slot("exclusive"):
            return self._run(config)

    def _run(self, config: BacktestConfig) -> BacktestResult:
        vbt = _get_vbt()
        run_id = uuid.uuid4().hex[:10]

        panel = self._load_panel(config.symbols, config.start, config.end, config.asset_type)
        if panel.empty:
            return BacktestResult(
                run_id=run_id,
                config=_config_to_dict(config),
                stats={"error": "no data"},
                equity_curve=[],
                trades=[],
                per_symbol_stats=[],
            )

        # 价格面板
        close = panel.pivot(index="date", columns="symbol", values="close")

        # 信号矩阵
        entries = self._build_signal_matrix(panel, config.entries, config)
        exits = self._build_signal_matrix(panel, config.exits, config)

        # 对齐 index/columns
        if not entries.empty:
            entries = entries.reindex_like(close).fillna(False).astype(bool)
        else:
            entries = pd.DataFrame(False, index=close.index, columns=close.columns)
        if not exits.empty:
            exits = exits.reindex_like(close).fillna(False).astype(bool)
        else:
            exits = pd.DataFrame(False, index=close.index, columns=close.columns)

        if not entries.any().any():
            return BacktestResult(
                run_id=run_id,
                config=_config_to_dict(config),
                stats={"error": "no buy signals"},
                equity_curve=[],
                trades=[],
                per_symbol_stats=[],
            )

        # T+1 适配:vectorbt 默认信号当根 K 撮合
        # close_t 撮合:维持默认
        # open_t+1 撮合:shift 信号 1 根 + 用 open 作为价
        if config.matching == "open_t+1":
            entries = entries.shift(1).fillna(False).astype(bool)
            exits = exits.shift(1).fillna(False).astype(bool)
            price = panel.pivot(index="date", columns="symbol", values="open")
        else:
            price = close

        # 跑回测
        try:
            # vectorbt 的 fees 是买卖统一费率, 无法区分"卖出腿印花税"。
            # 拆分口径下: 买入成本 = 佣金 + 过户费 + 滑点;
            #             卖出成本 = 佣金 + 过户费 + 印花 + 滑点。
            # 印花税只在卖出腿发生, 而每笔交易必然一买一卖 → 按双边摊薄:
            #   等效统一费率 = 佣金 + 过户费 + 印花/2 + 滑点
            # (与引擎 buy_cost_pct / sell_cost_pct 同源, 见 app/fees.py)。
            _cost = fee_rules.resolve_cost_model(
                asset_type=config.asset_type,
                commission_pct=config.commission_pct,
                stamp_tax_pct=config.stamp_tax_pct,
                transfer_fee_pct=config.transfer_fee_pct,
                min_commission=config.min_commission,
                slippage_bps=config.slippage_bps,
                fees_pct=config.fees_pct,
            )
            _effective_fees = (
                _cost.commission_pct + _cost.transfer_fee_pct + _cost.stamp_tax_pct / 2.0
            )
            pf_kwargs = dict(
                close=close,
                entries=entries,
                exits=exits,
                price=price,
                fees=_effective_fees,
                slippage=_cost.slippage_bps / 10000.0,
                freq="1D",
            )
            if config.stop_loss_pct is not None:
                pf_kwargs["sl_stop"] = abs(config.stop_loss_pct)
            if config.max_hold_days is not None:
                # vectorbt 没有内置 max-hold;用时间退出近似:入场后第 max_hold_days
                # 个交易日强制 exit, 与用户 exits 做 OR(保留原有信号退出)。
                forced_exits = _build_max_hold_exits(entries, config.max_hold_days)
                pf_kwargs["exits"] = (exits | forced_exits).astype(bool)

            pf = vbt.Portfolio.from_signals(**pf_kwargs)
        except Exception as e:  # noqa: BLE001
            logger.exception("vectorbt backtest failed")
            return BacktestResult(
                run_id=run_id,
                config=_config_to_dict(config),
                stats={"error": str(e)},
                equity_curve=[],
                trades=[],
                per_symbol_stats=[],
            )

        # 提取结果
        try:
            stats_series = pf.stats(silence_warnings=True)
            if isinstance(stats_series, pd.DataFrame):
                # 多列时取 agg
                stats_dict = stats_series.mean(numeric_only=True).to_dict()
            else:
                stats_dict = stats_series.to_dict()
        except Exception:  # noqa: BLE001
            stats_dict = {}

        # 净值曲线(组合平均)
        equity = pf.value().mean(axis=1) if isinstance(pf.value(), pd.DataFrame) else pf.value()
        equity_curve = [
            {"date": str(idx.date() if hasattr(idx, "date") else idx), "value": float(v)}
            for idx, v in equity.items() if pd.notna(v)
        ]

        # 交易记录
        try:
            trades_df = pf.trades.records_readable
            trades = trades_df.to_dict(orient="records") if not trades_df.empty else []
            # 字段名美化
            trades = [
                {
                    "symbol": t.get("Column", t.get("Symbol", "")),
                    "entry_date": str(t.get("Entry Timestamp", t.get("Entry Date", ""))),
                    "exit_date": str(t.get("Exit Timestamp", t.get("Exit Date", ""))),
                    "entry_price": float(t.get("Avg Entry Price", t.get("Avg. Entry Price", 0))),
                    "exit_price": float(t.get("Avg Exit Price", t.get("Avg. Exit Price", 0))),
                    "pnl_pct": float(t.get("Return", t.get("PnL %", 0))),
                    "duration": str(t.get("Duration", "")),
                }
                for t in trades
            ]
        except Exception:  # noqa: BLE001
            trades = []

        # 每标的统计
        per_symbol = []
        try:
            total_ret = pf.total_return()
            if isinstance(total_ret, pd.Series):
                for sym, ret in total_ret.items():
                    if pd.notna(ret):
                        per_symbol.append({"symbol": sym, "total_return": float(ret)})
        except Exception:  # noqa: BLE001
            pass

        result = BacktestResult(
            run_id=run_id,
            config=_config_to_dict(config),
            stats={k: _json_safe(v) for k, v in stats_dict.items()},
            equity_curve=equity_curve,
            trades=trades,
            per_symbol_stats=per_symbol,
        )

        # 落盘
        self._persist(result)
        return result

    def _persist(self, result: BacktestResult) -> None:
        out_dir = settings.data_dir / "backtest_results"
        out_dir.mkdir(parents=True, exist_ok=True)
        # 用 polars 写一份汇总
        summary = pl.DataFrame({
            "run_id": [result.run_id],
            "stats_json": [str(result.stats)],
            "n_trades": [len(result.trades)],
        })
        summary.write_parquet(out_dir / f"run_id={result.run_id}.parquet")

    def get_result(self, run_id: str) -> BacktestResult | None:
        # Phase 1:只保留近似落盘,完整结果保存在内存的近期 cache 中
        # 简化:重新 run 比缓存复杂结果代价小,暂不实现 get_result
        return None


def _config_to_dict(c: BacktestConfig) -> dict:
    cost = fee_rules.resolve_cost_model(
        asset_type=getattr(c, "asset_type", "stock"),
        commission_pct=c.commission_pct,
        stamp_tax_pct=c.stamp_tax_pct,
        transfer_fee_pct=getattr(c, "transfer_fee_pct", None),
        min_commission=getattr(c, "min_commission", None),
        slippage_bps=c.slippage_bps,
        fees_pct=c.fees_pct,
    )
    return {
        "symbols": c.symbols,
        "start": str(c.start),
        "end": str(c.end),
        "entries": c.entries,
        "exits": c.exits,
        "stop_loss_pct": c.stop_loss_pct,
        "max_hold_days": c.max_hold_days,
        "fees_pct": c.fees_pct,
        "slippage_bps": c.slippage_bps,
        # 解析后的实际成本口径 (含按品种取的印花税/过户费), 供结果审计核对。
        "cost_model": cost.describe(),
        "matching": c.matching,
    }


def _json_safe(v):
    # 非有限浮点 (inf / NaN) 必须先于原生标量分支拦下: Starlette 的 JSONResponse 用
    # json.dumps(allow_nan=False) 渲染, 漏一个就是整个响应 500。pf.stats() 经
    # pandas Series.to_dict() 出来时 numpy 标量已被装箱成原生 float (全胜时
    # Profit Factor = inf, 零波动时 Sharpe = NaN), 两条分支都要覆盖。
    if isinstance(v, (float, np.floating)) and not math.isfinite(float(v)):
        return None
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    if isinstance(v, (np.floating, np.integer)):
        return float(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)
