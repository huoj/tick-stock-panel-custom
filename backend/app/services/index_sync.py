"""指数 / ETF 数据同步服务。

标的列表优先用免费的 exchanges.get_instruments(type=index/etf) 拉取
(None/Free 档均可用,无需 quote.pool 权限);付费档可额外用
quotes.get_by_universes 作为补充来源。日K统一走 klines.batch。
"""
from __future__ import annotations

import gc
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import polars as pl

from app.indicators.pipeline import compute_enriched
from app.services import kline_sync, preferences
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.client import get_client
from app.tickflow.rate_limits import chunked, min_batch, resolve_limit, sleep_between_batches
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

# exchanges.get_instruments 查询的交易所(沪深京)
_EXCHANGES = ["SH", "SZ", "BJ"]


@dataclass(frozen=True)
class DailyCoverage:
    """某个交易日标的覆盖情况 —— 判断"当天数据是否真的同步到位"。

    为什么不能只看总行数: 拉取窗口含历史, 一批 429 被拒后其余批次仍会带回
    历史行, 于是 total_rows > 0 而**当天一行都没进** (2026-11-09 ETF 实测)。
    判据必须是「目标日实际覆盖了多少标的」。
    """

    target_date: date
    expected: int
    covered: int
    missing: int
    #: 展示用资产标签 ("ETF"/"指数"), 供 describe() 拼中文说明
    asset_label: str = "指数/ETF"
    #: 判失败的覆盖率下限 (0.9 = 覆盖不足基准的 90% 才算异常)。
    #: 停牌股会让覆盖数小幅下降, 卡 100% 会天天误报。
    min_ratio: float = 0.9

    @property
    def complete(self) -> bool:
        if self.expected <= 0:
            return True
        # 覆盖数达到基准的 min_ratio 即视为正常 (剩余缺口是停牌/退市, 非同步失败)
        return self.covered >= self.expected * self.min_ratio

    def describe(self, asset_label: str | None = None) -> str:
        """给调用方拼 stage_errors / 日志的中文说明 (仅用于异常场景)。"""
        label = asset_label or self.asset_label
        return (
            f"{label} {self.target_date} 日K覆盖异常: 仅 {self.covered}/{self.expected} 只"
            f"(缺失 {self.missing}, 低于 {self.min_ratio:.0%} 阈值)"
        )

    def summary(self) -> str:
        """中性描述 (正常/异常都适用), 供正常日志使用。"""
        state = "完整" if self.complete else "异常"
        return f"{self.asset_label} {self.target_date} 日K{state}: {self.covered}/{self.expected} 只"


def daily_coverage(
    frame: pl.DataFrame,
    *,
    expected: int,
    target_date: date,
    failed_symbols: list[str] | None = None,
    asset_label: str = "指数/ETF",
) -> DailyCoverage:
    """统计 ``frame`` 里目标交易日的标的覆盖数。

    - ``expected`` 是本次应当同步到的标的数 (0 表示无可同步标的, 不判失败);
    - ``failed_symbols`` 是分块失败已知的标的, 与覆盖数取并集后算缺口
      (避免"失败但恰好也在旧数据里"被算成已覆盖);
    - 缺 ``date`` 列的异常帧视为零覆盖, 不抛异常 (由调用方按不完整处理)。
    """
    if expected <= 0:
        return DailyCoverage(
            target_date=target_date, expected=0, covered=0, missing=0,
            asset_label=asset_label,
        )

    covered = 0
    if not frame.is_empty() and "date" in frame.columns:
        today_rows = frame.filter(pl.col("date") == target_date)
        if not today_rows.is_empty() and "symbol" in today_rows.columns:
            covered = int(today_rows["symbol"].n_unique())

    # 已知失败的标的即使出现在 covered 里也算缺口 —— 它们本轮的价可能是旧的
    failed = {s for s in (failed_symbols or []) if s}
    if failed:
        today_symbols = set()
        if covered and "symbol" in frame.columns:
            today_symbols = set(
                frame.filter(pl.col("date") == target_date)["symbol"].to_list()
            )
        covered -= len(failed & today_symbols)
        # 失败但不在覆盖里 -> 天然计入缺口, 无需额外减

    missing = max(expected - covered, 0)
    return DailyCoverage(
        target_date=target_date, expected=expected, covered=covered, missing=missing,
        asset_label=asset_label,
    )


def _quotes_to_index_instruments(resp) -> pl.DataFrame:
    """将 TickFlow quotes 响应(get_by_universes)规范为指数 instruments。

    付费档(Starter+)的补充来源,免费档用不到。
    """
    if resp is None:
        return pl.DataFrame()

    if isinstance(resp, pl.DataFrame):
        df = resp
    elif hasattr(resp, "columns"):
        df = pl.from_pandas(resp.reset_index() if hasattr(resp, "reset_index") else resp)
    else:
        rows: list[dict] = []
        for q in resp or []:
            item = q if isinstance(q, dict) else {}
            ext = item.get("ext") or {}
            symbol = item.get("symbol")
            if not symbol:
                continue
            rows.append({
                "symbol": str(symbol),
                "name": ext.get("name") or item.get("name") or str(symbol),
            })
        df = pl.DataFrame(rows)

    if df.is_empty() or "symbol" not in df.columns:
        return pl.DataFrame()

    rename = {"ts_code": "symbol"}
    df = df.rename({k: v for k, v in rename.items() if k in df.columns})

    if "name" not in df.columns:
        if "ext" in df.columns:
            df = df.with_columns(pl.col("symbol").cast(pl.Utf8).alias("name"))
        else:
            df = df.with_columns(pl.col("symbol").cast(pl.Utf8).alias("name"))

    result = df.select([
        pl.col("symbol").cast(pl.Utf8),
        pl.col("name").cast(pl.Utf8),
    ]).with_columns([
        pl.col("symbol").str.split(".").list.first().alias("code"),
        pl.lit("index").alias("asset_type"),
    ])
    return result.unique(subset=["symbol"], keep="last").sort("symbol")


def _fetch_instruments_by_type(instrument_type: str, asset_type_label: str) -> pl.DataFrame:
    """用免费的 exchanges.get_instruments 拉取指定类型的标的列表。

    None/Free 档均可使用(标的信息查询免费开放)。
    instrument_type: 'index' / 'etf'
    asset_type_label: 写入 instruments 表的 asset_type 标记('index' / 'etf')
    """
    tf = get_client()
    rows: list[dict] = []
    for ex in _EXCHANGES:
        try:
            items = tf.exchanges.get_instruments(ex, instrument_type=instrument_type)
            for it in items or []:
                item = it if isinstance(it, dict) else {}
                symbol = item.get("symbol")
                if not symbol:
                    continue
                rows.append({
                    "symbol": str(symbol),
                    "name": item.get("name") or str(symbol),
                })
        except Exception as e:  # noqa: BLE001
            logger.warning("get_instruments(%s, type=%s) failed: %s", ex, instrument_type, e)

    if not rows:
        return pl.DataFrame()

    return (
        pl.DataFrame(rows)
        .with_columns([
            pl.col("symbol").str.split(".").list.first().alias("code"),
            pl.lit(asset_type_label).alias("asset_type"),
        ])
        .unique(subset=["symbol"], keep="last")
        .sort("symbol")
    )


def sync_index_instruments(
    repo: KlineRepository,
    pull_index: bool = True,
    pull_etf: bool = True,
) -> int:
    """同步指数 / ETF 标的维表,返回标的总数。

    新版物理分开保存: 指数写 instruments_index, ETF 写 instruments_etf。
    读取层仍兼容旧版 instruments_index 中 asset_type='etf' 的历史数据。
    """
    index_parts: list[pl.DataFrame] = []
    etf_parts: list[pl.DataFrame] = []

    # 1) 免费通道:按开关分别拉 index / etf
    if pull_index:
        index_df = _fetch_instruments_by_type("index", "index")
        if not index_df.is_empty():
            index_parts.append(index_df)
    if pull_etf:
        etf_df = _fetch_instruments_by_type("etf", "etf")
        if not etf_df.is_empty():
            etf_parts.append(etf_df)

    # 2) 付费补充:Starter+ 用 get_by_universes 补指数(仅当开启指数拉取)
    if pull_index:
        capset = None
        try:
            from app.tickflow import policy
            capset = policy.detect_capabilities(force=False)
        except Exception:  # noqa: BLE001
            pass
        if capset is not None and capset.has(Cap.QUOTE_POOL):
            tf = get_client()
            for kwargs in (
                {"universes": ["CN_Index"]},
                {"universes": ["CN_Index"], "as_dataframe": False},
            ):
                try:
                    resp = tf.quotes.get_by_universes(**kwargs)
                    if resp is not None and len(resp) > 0:
                        sup = _quotes_to_index_instruments(resp)
                        if not sup.is_empty():
                            index_parts.append(sup)
                        break
                except Exception as e:  # noqa: BLE001
                    logger.debug("CN_Index universe supplement failed: %s", e)

    total = 0
    if index_parts:
        index_inst = pl.concat(index_parts, how="diagonal_relaxed").unique(subset=["symbol"], keep="last").sort("symbol")
        if not index_inst.is_empty():
            repo.save_index_instruments(index_inst)
            total += index_inst.height
    if etf_parts:
        etf_inst = pl.concat(etf_parts, how="diagonal_relaxed").unique(subset=["symbol"], keep="last").sort("symbol")
        if not etf_inst.is_empty():
            repo.save_etf_instruments(etf_inst)
            total += etf_inst.height

    if total == 0:
        logger.warning("指数/ETF 标的列表为空(pull_index=%s, pull_etf=%s)", pull_index, pull_etf)
        return 0
    repo.refresh_index_views()
    logger.info("指数/ETF 标的同步完成: %d 只", total)
    return total


def sync_etf_instruments(repo: KlineRepository) -> int:
    """单独同步 ETF 标的维表(返回 ETF 数量)。"""
    etf_df = _fetch_instruments_by_type("etf", "etf")
    if etf_df.is_empty():
        return 0
    repo.save_etf_instruments(etf_df)
    repo.refresh_index_views()
    return etf_df.height


def _iter_custom_daily_frames(provider, symbols, asset_type, start_time, end_time, on_chunk_done):
    """自定义源日K帧迭代器: iter_daily 流式优先, 仅实现 get_daily 的源包装为单帧。

    asset_type 按内部契约传 "index"/"etf"; 只服务股票的源 (fuyao/tdxquant/amber
    对 asset_type≠stock 一律空手而归) 由此自然表达「不支持」, 由调用方回退。"""
    yield from kline_sync.iter_custom_daily_frames(
        provider, symbols, asset_type=asset_type, start_time=start_time,
        end_time=end_time, on_chunk_done=on_chunk_done,
    )


def _sync_custom_daily(
    repo: KlineRepository,
    symbols: list[str],
    *,
    asset_type: str,
    asset_label: str,
    start_time: datetime,
    end_time: datetime,
    on_chunk_done: Callable[[int, int], None] | None,
    persist_frame: Callable[[pl.DataFrame], int],
) -> int:
    """日K源为自定义 provider 时接管指数/ETF 日K同步 (#502)。

    与个股 sync_and_persist_daily_batch 同一路由语义, 路由判定在 TickFlow 能力
    检查之前 —— 纯自定义源部署 (未配置 TickFlow) 也能同步指数/ETF。

    返回 -1 表示「本次不由自定义源接管」, 调用方继续 TickFlow 原路径:
    - 日K源是 tickflow, 或该源未声明 daily 数据集;
    - 声明了但对该资产类型空手而归 —— 插件契约本就如此 (tdxquant 注释:
      "asset_type 仅接受 stock, 其余空手而归(上层回退)"), 告警后回退,
      与修复前行为一致, 不静默吞掉。

    persist_frame 接收一帧原始日K并完成 append_*_daily + enriched 落盘,
    返回行数 —— 指数/ETF 各自复用与 TickFlow 路径相同的落盘逻辑, 不另起一套。
    """
    provider_name = preferences.get_daily_data_provider()
    if provider_name == "tickflow":
        return -1
    from app.data_providers import custom as custom_sources
    if not custom_sources.provider_has_dataset(provider_name, "daily"):
        return -1

    provider = custom_sources.get_provider(provider_name)
    total_rows = 0
    for raw in _iter_custom_daily_frames(
        provider, symbols, asset_type, start_time, end_time, on_chunk_done,
    ):
        if raw is None or raw.is_empty():
            continue
        total_rows += persist_frame(raw)
        logger.info("%s daily synced (custom %s): +%d rows", asset_label, provider_name, raw.height)
        del raw
        gc.collect()
    if total_rows > 0:
        repo.refresh_index_views()
        return total_rows
    logger.warning(
        "自定义源 %s 未返回%s日K数据 (源仅服务股票时属预期), 本轮回退 TickFlow",
        provider_name, asset_label,
    )
    return -1


def sync_and_persist_index_daily(
    repo: KlineRepository,
    capset: CapabilitySet,
    count: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    symbols_override: list[str] | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
    coverage_out: list[DailyCoverage] | None = None,
) -> int:
    """同步指数/ETF 日K到独立 parquet,并计算 enriched。

    symbols_override 非空时,只拉这些代码(跳过 instruments 表),用于自定义范围。
    否则取 index_instruments 表全量(指数+ETF 合并存储)。
    on_chunk_done(current, total) 每个批次完成后回调。

    coverage_out: 可选出参, 语义同 sync_and_persist_etf_daily。
    """
    if symbols_override:
        symbols = sorted(set(s for s in symbols_override if s))
        if not symbols:
            return 0
    else:
        instruments = repo.get_index_instruments()
        if instruments.is_empty():
            sync_index_instruments(repo, pull_index=True, pull_etf=False)
            instruments = repo.get_index_instruments()
        if not instruments.is_empty() and "asset_type" in instruments.columns:
            instruments = instruments.filter(pl.col("asset_type") != "etf")
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return 0
        symbols = sorted(set(instruments["symbol"].to_list()))

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    def _persist_index_frame(raw: pl.DataFrame) -> int:
        repo.append_index_daily(raw)
        enriched = compute_enriched(raw, factors=None, instruments=None)
        repo.append_index_enriched(enriched)
        del enriched
        return raw.height

    # 自定义日K源优先接管指数日K (#502): 路由判定在 TickFlow 能力检查之前,
    # 纯自定义源部署 (未配置 TickFlow) 也能同步; 空手而归时回退下方 TickFlow 路径。
    custom_rows = _sync_custom_daily(
        repo, symbols,
        asset_type="index", asset_label="指数",
        start_time=start_time, end_time=end_time,
        on_chunk_done=on_chunk_done,
        persist_frame=_persist_index_frame,
    )
    if custom_rows >= 0:
        return custom_rows

    if not capset.has(Cap.KLINE_DAILY_BATCH):
        kline_sync._log_cap_unavailable("daily", "指数/ETF 日K同步")
        return 0
    limit = resolve_limit(capset, Cap.KLINE_DAILY_BATCH)
    batch_size = min_batch(preferences.get_index_daily_batch_size(), limit)

    total_rows = 0
    failed_syms: list[str] = []
    chunks = chunked(symbols, batch_size)
    for i, chunk in enumerate(chunks):
        sleep_between_batches(i, limit.rpm)
        raw = kline_sync.sync_daily_batch(
            chunk,
            count=count,
            batch_size=None,
            start_time=start_time,
            end_time=end_time,
            failed_out=failed_syms,
        )
        if raw.is_empty():
            continue

        total_rows += _persist_index_frame(raw)
        logger.info("index/etf daily synced: %d/%d chunks, +%d rows", i + 1, len(chunks), raw.height)
        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))
        del raw
        gc.collect()
    repo.refresh_index_views()
    _report_daily_coverage(
        repo, "index", "kline_index_daily", symbols, end_time, failed_syms, coverage_out,
    )
    return total_rows


def _load_etf_factors(repo: KlineRepository) -> pl.DataFrame:
    factor_path = repo.store.data_dir / "adj_factor_etf" / "all.parquet"
    if not factor_path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(factor_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("ETF 复权因子读取失败: %s", e)
        return pl.DataFrame()


def etf_adj_factor_window_start(adj_path, history_start: datetime) -> datetime:
    """ETF 除权拉取窗口起点。

    本地还没有因子文件时与日K历史对齐 (调用方传入 today-365 或最早分区日),
    不要只拉最近 30 天: 拆分落在窗口外则全年日K都不复权。已有文件则从最新
    事件日续拉。
    """
    if not adj_path.exists():
        return history_start
    try:
        max_date = pl.scan_parquet(adj_path).select(pl.col("trade_date").max()).collect().item()
    except Exception as e:
        logger.warning("读取 ETF 除权因子最新日期失败, 回退日K历史起点: %s", e)
        return history_start
    if max_date is None:
        return history_start
    if isinstance(max_date, str):
        stored = datetime.combine(datetime.fromisoformat(max_date).date(), datetime.min.time())
    elif isinstance(max_date, datetime):
        stored = datetime.combine(max_date.date(), datetime.min.time())
    else:
        stored = datetime.combine(max_date, datetime.min.time())
    return stored


def _load_local_etf_daily(repo: KlineRepository, symbols: list[str]) -> pl.DataFrame:
    """读本批 symbol 的完整本地 ETF 日K, 供前复权改写历史价。"""
    daily_dir = repo.store.data_dir / "kline_etf_daily"
    if not daily_dir.exists() or not any(daily_dir.glob("date=*")):
        return pl.DataFrame()
    from app.parquet import scan_daily_parquet
    try:
        return (
            scan_daily_parquet((daily_dir / "**" / "*.parquet").as_posix())
            .filter(pl.col("symbol").is_in(symbols))
            .collect()
        )
    except Exception as e:
        logger.warning("读取本地 ETF 日K失败: %s", e)
        return pl.DataFrame()


def sync_etf_adj_factor(
    symbols: list[str],
    repo: KlineRepository,
    capset: CapabilitySet,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    on_chunk_done=None,
) -> tuple[int, list[str]]:
    """同步 ETF 复权因子；失败由调用方降级为 warning。"""
    return kline_sync.sync_adj_factor(
        symbols,
        repo,
        capset,
        start_time=start_time,
        end_time=end_time,
        on_chunk_done=on_chunk_done,
        asset_type="etf",
    )


def _previous_coverage(
    repo: KlineRepository, table: str, target: date, *, lookback_days: int = 10,
) -> int | None:
    """取 target 之前最近一个有数据分区的标的数, 作为当日覆盖基准。

    找不到 (首次同步/全部为空) 返回 None, 由调用方回退到标的表全量。
    """
    base = repo.store.data_dir / table
    if not base.exists():
        return None
    for delta in range(1, lookback_days + 1):
        part = base / f"date={(target - timedelta(days=delta)).isoformat()}" / "part.parquet"
        if not part.exists():
            continue
        try:
            df = pl.read_parquet(part, columns=["symbol"])
        except Exception:  # noqa: BLE001 — 坏分区跳过, 继续往前找
            continue
        if not df.is_empty():
            return int(df["symbol"].n_unique())
    return None


def _report_daily_coverage(
    repo: KlineRepository,
    asset_type: str,
    table: str,
    symbols: list[str],
    end_time: datetime,
    failed_syms: list[str],
    coverage_out: list[DailyCoverage] | None,
) -> DailyCoverage | None:
    """校验目标交易日落在磁盘上的真实覆盖, 缺失则 WARNING 并回传出参。

    读磁盘而不是复用内存帧: 落盘走的是 merge-upsert, 内存帧的行数与最终
    分区里的标的数不是一回事 (旧分区已含历史行)。判据要落在"这一天的分区
    里到底有多少只标的"。
    """
    asset_label = {"etf": "ETF", "index": "指数", "stock": "A股"}.get(asset_type, "指数/ETF")
    target = end_time.date()
    frame = pl.DataFrame()
    part = repo.store.data_dir / table / f"date={target.isoformat()}" / "part.parquet"
    if part.exists():
        try:
            frame = pl.read_parquet(part, columns=["symbol", "date"])
        except Exception as e:  # noqa: BLE001 — 读失败按零覆盖处理, 不阻断管道
            logger.warning("%s 分区 %s 读取失败, 按零覆盖处理: %s", asset_type, target, e)

    # 基准取"上一有数据交易日的实际覆盖", 不用标的表全量:
    # ETF 里常有一批从未上市/已退市的代码 (实测 1763 只里有 44 只从无任何数据),
    # 用全量当分母会天天误报。停牌股会小幅掉几个, 属正常波动。
    baseline = _previous_coverage(repo, table, target)

    cov = daily_coverage(
        frame, expected=baseline or len(symbols), target_date=target,
        failed_symbols=failed_syms, asset_label=asset_label,
    )
    if cov.complete:
        logger.info("%s", cov.summary())
    else:
        logger.warning("%s", cov.describe())
    if coverage_out is not None:
        coverage_out.append(cov)
    return cov


def check_daily_coverage(
    repo: KlineRepository,
    asset_type: str,
    table: str,
    *,
    target: date,
    symbols: list[str] | None = None,
    failed_symbols: list[str] | None = None,
) -> DailyCoverage:
    """只读校验某表在 ``target`` 交易日的真实覆盖 (不落盘, 不抛异常)。

    与管道内 ``_report_daily_coverage`` 同源同判据, 供「补拉前置检查」复用:
    盘后管道默认 15:35, 而免费档供应商的当日日K要晚 1~2 小时才发布, 补拉任务
    需要先判断"今天的数据到底到没到", 以免对着未发布的日K反复重跑整条管道。
    """
    cov = _report_daily_coverage(
        repo, asset_type, table, list(symbols or []),
        datetime.combine(target, datetime.min.time()),
        list(failed_symbols or []), None,
    )
    return cov or DailyCoverage(
        target_date=target, expected=0, covered=0, missing=0,
        asset_label={"etf": "ETF", "index": "指数", "stock": "A股"}.get(asset_type, "指数/ETF"),
    )


def sync_and_persist_etf_daily(
    repo: KlineRepository,
    capset: CapabilitySet,
    count: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    symbols_override: list[str] | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
    coverage_out: list[DailyCoverage] | None = None,
) -> int:
    """同步 ETF 日K到独立 kline_etf_* parquet,并计算 ETF enriched。
    on_chunk_done(current, total) 每个批次完成后回调。

    coverage_out: 可选出参。传入 list 时把本次的目标日覆盖情况追加进去,
    供调用方判断"当天数据是否真的到位"并计入 stage_errors。
    不传时行为与旧版一致 (只返回总行数)。
    """
    if symbols_override:
        symbols = sorted(set(s for s in symbols_override if s))
    else:
        instruments = repo.get_etf_instruments()
        if instruments.is_empty():
            sync_etf_instruments(repo)
            instruments = repo.get_etf_instruments()
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return 0
        symbols = sorted(set(instruments["symbol"].to_list()))
    if not symbols:
        return 0

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    factors = _load_etf_factors(repo)

    def _persist_etf_frame(raw: pl.DataFrame) -> int:
        frame_symbols = raw["symbol"].unique().to_list()
        repo.append_etf_daily(raw)
        batch_factors = (
            factors.filter(pl.col("symbol").is_in(frame_symbols))
            if not factors.is_empty() else factors
        )
        # 前复权 ratio = cum/total, 新除权事件会改写全部历史价。只用本次拉取
        # 窗口算 enriched 时, 拆分日前的分区停在未复权价, 日K 留下跳空。
        local = _load_local_etf_daily(repo, frame_symbols)
        hist = local if not local.is_empty() else raw
        # ETF 使用复权和通用技术指标; 不传 instruments, 避免套用 A股涨跌停/连板逻辑。
        enriched = compute_enriched(hist, factors=batch_factors, instruments=None)
        repo.append_etf_enriched(enriched)
        del enriched, local, hist
        return raw.height

    # 自定义日K源优先接管 ETF 日K (#502), 语义与上方指数分支一致
    custom_rows = _sync_custom_daily(
        repo, symbols,
        asset_type="etf", asset_label="ETF",
        start_time=start_time, end_time=end_time,
        on_chunk_done=on_chunk_done,
        persist_frame=_persist_etf_frame,
    )
    if custom_rows >= 0:
        return custom_rows

    if not capset.has(Cap.KLINE_DAILY_BATCH):
        kline_sync._log_cap_unavailable("daily", "ETF 日K同步")
        return 0
    limit = resolve_limit(capset, Cap.KLINE_DAILY_BATCH)
    batch_size = min_batch(preferences.get_index_daily_batch_size(), limit)

    total_rows = 0
    failed_syms: list[str] = []
    chunks = chunked(symbols, batch_size)
    for i, chunk in enumerate(chunks):
        sleep_between_batches(i, limit.rpm)
        raw = kline_sync.sync_daily_batch(
            chunk,
            count=count,
            batch_size=None,
            start_time=start_time,
            end_time=end_time,
            failed_out=failed_syms,
        )
        if raw.is_empty():
            continue

        total_rows += _persist_etf_frame(raw)
        logger.info("etf daily synced: %d/%d chunks, +%d rows", i + 1, len(chunks), raw.height)
        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))
        del raw
        gc.collect()
    repo.refresh_index_views()
    _report_daily_coverage(
        repo, "etf", "kline_etf_daily", symbols, end_time, failed_syms, coverage_out,
    )
    return total_rows
