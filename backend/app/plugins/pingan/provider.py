"""平安证券 skill 平台内置数据源 provider (行情查询技能)。

平台: https://stock.pingan.com/huodong/aiskill/skillPage/
接口: https://ai.stock.pingan.com/restapi/hangqing/adapter/hangQing/*

## 为什么需要这个源

TSP 现有三个源在免费档都不提供 ETF 当日数据 (2026-11 实测):
  - tickflow 免费档: {"code":"FREE_TIER_RESTRICTED"} 明确不支持实时行情
  - fuyao: 路径只覆盖 A 股, ETF 报 Unknown thscode
  - tdxquant: get_daily(asset_type="etf") 返回 0 行
结果 `kline_etf_daily` 长期落后 (股票已有当日, ETF 停在两天前)。

平安快照实测能拿到 ETF 的现价/涨跌/昨收/换手 + ETF 独有字段
(IOPV、折溢价、跟踪指数、基金规模), 这是本项目此前完全没有的数据。

## 能力边界 (关键, 不可越界)

只声明 `etf` 数据集 (ETF 实时快照), **不声明 realtime/daily**:

1. 不声明 realtime: 平安 quote 实测一批 8 个代码返回 8 条 (文档写 5),
   全市场 1719 只 ETF 一轮就要打到额度上限; 5583 只股票更不可能。
   抢占 realtime 会让股票全市场轮询直接空转。
2. 不声明 daily: 平安 `klineData` 返回的是**前复权**价 (响应体 adjust 字段
   实测为"前复权", 且无参数可切换 —— adjust/none/0/nofq 全部被忽略,
   仍返回前复权)。2026-11 实测与东财 fqt=0(不复权) 对比:
     600941.SH 180 个交易日中 149 天背离, 最大 -4.71%
     000651.SZ 126 天背离, 最大 -5.20%
     510300.SH 除息后当日即出现背离
   内部契约 (CONTRIBUTING §3.2) 要求日K入库为不复权原始价, 前复权价进库
   会污染 raw 基准、静默算错复权因子与全部指标。故日K一列都不取。
   (快照 get_etf_realtime 的当日 OHLC 是原始价 —— 前复权的锚点是"最新",
   当日价恒等于 raw; 实测今日 bar 与 quote 逐字段一致, 可安全作为当日值。)

历史 ETF 日K缺口不在本插件范围内: 那需要不复权的历史序列, 平安不提供。

## 单位与口径 (全部实测, 不按字段名推断)

| 平安字段 | 平安原始 | 内部契约 | 处理 |
|---|---|---|---|
| change_pct | "0.22%" 百分数字符串 | 小数制 0.0022 | 剥 % 后 /100 |
| turnover_pct | "0.50%" | 小数制 0.005 | 剥 % 后 /100 |
| volume | 股 | 手 | floor(/100) |
| amount | 元 | 元 | 透传 |
| price/prev_close/open/high/low | 元 | 元 | 透传 |

所有数值在响应里都是**字符串**, 且百分比带 `%` 后缀。直接 float('0.22%')
抛 ValueError, 被 except 吞掉后静默变 None —— 必须先剥后缀 (交接文档踩坑 #6)。

盘前 (9:30 前) 实测 price="0"/change="0", 且 high/low 亦为 0;
此时必须回退 prev_close, 否则 ETF 显示 0 元、0.00% 假跌停 (踩坑 #7)。

## 限频

实测: 429 返回体 {"error_code":"RATE_LIMIT_EXCEEDED"}, HTTP 状态也是 429。
0.15s 间隔连打 12 次会间歇 429 (12 次里 1 次)。因此:
  - 批次之间强制节流 (MIN_INTERVAL_S), 且 429 按退避重试有限次
  - 单次调用上限按实测 5 只保守取值 (文档口径), 不试探服务端未公开的更大上限
额度 2000/日 (用户确认)。ETF 持仓 3 只时一轮永远只发 1 个请求。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from app.data_providers.base import ProviderRequestError
from app.plugins.pingan import client as pingan_client
from app.plugins.pingan.client import PingAnClient, PingAnError, PingAnRateLimitError

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余数据集 provider_has_dataset 返回 False → 回退默认源
_DATASETS = ("etf",)

API_KEY_ENV = "PINGAN_SKILL_APIKEY"
SECRETS_FIELD = "pingan_api_key"  # UI 配置的 Key 存 secrets.json, 优先级高于 .env


def get_api_key() -> str:
    from app import secrets_store

    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def availability() -> tuple[bool, str]:
    """loader 启动自检: 配了 Key 才注册为可切换数据源。不抛异常、不联网。"""
    if get_api_key():
        return True, "ok"
    return False, f"缺少 API Key(可在下方输入框直接填写,或配置环境变量 {API_KEY_ENV})"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """用候选 Key 实探一次快照接口(先探后存, 对齐 /plugin-key 语义)。不落盘。"""
    client = None
    try:
        client = PingAnClient(api_key=api_key, timeout=10.0)
        client.quote(["SZ159682"])
        return True, "ok"
    except PingAnRateLimitError:
        # 限流不代表 Key 无效: 服务端已通过鉴权才会走配额判断。
        return True, "ok(接口限流, 已通过鉴权)"
    except PingAnError as e:
        return False, f"Key 无效或网络失败: {e}"
    finally:
        if client is not None:
            client.close()


@dataclass
class _PingAnConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "pingan"
    display_name: str = "平安证券"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _num(value) -> float | None:
    """平安数值全是字符串; 空串/"-"/"--"/"null" 返回 None, 不伪造 0。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value).strip()
    if text in ("", "-", "--", "null", "None"):
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _pct(value) -> float | None:
    """百分数字符串 → 百分数数值本身 ("0.22%" → 0.22)。不做 /100。

    调用方按内部契约决定是否 /100: 实时入口 change_pct/turnover_rate 为小数制,
    所以这里保持百分数原值, 由 _map_quote_row 显式换算。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    return _num(str(value).strip().rstrip("%"))


def to_pa_symbol(symbol: str) -> str:
    """内部代码 → 平安代码: `159682.SZ` → `SZ159682`。

    平安要「市场前缀 + 6 位代码」, 明确不接受裸代码或 `600519.SH` 写法。
    北交所 (BJ) 平安不支持 (实测返回 not_found), 由调用方过滤。
    """
    text = str(symbol or "").strip().upper()
    code, _, market = text.partition(".")
    if not market:
        return text
    return f"{market}{code}"


def from_pa_symbol(code: str) -> str | None:
    """平安代码 → 内部代码: `SZ159682` → `159682.SZ`。识别不了返回 None。"""
    text = str(code or "").strip().upper()
    if len(text) < 3:
        return None
    market, digits = text[:2], text[2:]
    if market not in ("SH", "SZ", "BJ") or not digits.isdigit():
        return None
    return f"{digits}.{market}"


def _map_quote_row(row: dict, fetched_ms: int) -> dict | None:
    """平安快照行 → 内部 realtime record。缺字段置 None, 不启发式补全。"""
    symbol = from_pa_symbol(row.get("code"))
    if symbol is None:
        return None

    prev_close = _num(row.get("prev_close"))
    price = _num(row.get("price"))
    open_ = _num(row.get("open"))
    high = _num(row.get("high"))
    low = _num(row.get("low"))

    # 盘前/停牌: 平安给 price=0 且 OHLC 全 0。用昨收兜底最新价, 否则页面显示
    # 0 元 + 0.00% 假跌停 (交接文档踩坑 #7)。OHLC 为 0 时同样回退昨收,
    # 与 quote_service._build_daily 的容错口径一致 (0 → close)。
    stale = price is None or price == 0
    # 没有昨收可退时置 None: 宁可显示"无数据", 也不下发 0 元假价格。
    last_price = prev_close if stale and prev_close else (None if stale else price)

    change_pct = _pct(row.get("change_pct"))
    if change_pct is not None:
        change_pct = change_pct / 100.0  # 百分数 → 小数制 (契约 §3.1)
    change_amount = _num(row.get("change"))
    if change_amount is None and last_price is not None and prev_close not in (None, 0):
        change_amount = last_price - prev_close
    if change_pct is None and change_amount is not None and prev_close not in (None, 0):
        change_pct = change_amount / prev_close

    volume = _num(row.get("volume"))
    turnover = _pct(row.get("turnover_pct"))
    if turnover is not None:
        turnover = turnover / 100.0  # 百分数 → 小数制 (入口契约)

    def _price_or_fallback(value: float | None) -> float | None:
        """盘前 OHLC 全 0 → 回退最新价, 避免零价蜡烛 (与 _build_daily 同口径)。"""
        if value is None or value == 0:
            return last_price
        return value

    return {
        "symbol": symbol,
        "name": (str(row.get("name") or "").strip() or None),
        "last_price": last_price,
        "prev_close": prev_close,
        "open": _price_or_fallback(open_),
        "high": _price_or_fallback(high),
        "low": _price_or_fallback(low),
        # 平安 volume 为股 → 内部契约手 (1 手 = 100 股)
        "volume": math.floor(volume / 100.0) if volume is not None else None,
        "amount": _num(row.get("amount")),
        "change_pct": change_pct,
        "change_amount": change_amount,
        # 快照未提供振幅; 不据 OHLC 反推 (停牌/盘前会算出假 0)
        "amplitude": None,
        "turnover_rate": turnover,
        "timestamp": fetched_ms,
        "session": None,
        # ETF 独有字段 (此前项目完全没有): 供上层扩展, 不参与日K落盘
        "iopv": _num(row.get("iopv")),
        "premium_rate": _pct(row.get("premium_rate")),
        "tracking_index": (str(row.get("tracking_index") or "").strip() or None),
        "fund_size": _num(row.get("fund_size")),
        "pe_ttm": _num(row.get("pe_ttm")),
        "pb": _num(row.get("pb")),
    }


class PingAnProvider:
    """平安证券数据源。当前只提供 ETF 实时快照 (etf 数据集)。"""

    name = "pingan"
    builtin = True

    def __init__(self) -> None:
        self.config = _PingAnConfig()
        self._client: PingAnClient | None = None
        self._lock = threading.Lock()
        # get_etf_realtime 的最近一次调用是否完整拿到所有请求标的。
        # 限流/错误 break 时会置 False —— 上层据此判定「半截数据不是可信成功」,
        # 不把它当成功缓存 60s、不标记整分区覆盖 (BUG-C)。
        self.last_etf_fetch_complete: bool = True

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _get_client(self) -> PingAnClient:
        with self._lock:
            if self._client is None:
                self._client = PingAnClient(api_key=get_api_key())
            return self._client

    # ---- etf 数据集 (ETF 实时快照) ----
    def get_etf_realtime(
        self, symbols: list[str] | None = None, *, asset_type: str = "etf",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> list[dict]:
        """按需拉取 ETF 实时快照。

        symbols 为空 → 返回 [] (本插件不做全市场枚举: 额度不允许, 也不应抢拍)。
        失败软返回 [] + warning: 行情线程不能被单源故障打断 (契约同 get_realtime)。
        北交所代码平安不支持, 直接过滤, 避免整批 not_found 连坐。
        """
        wanted: list[str] = []
        for symbol in symbols or []:
            text = str(symbol or "").strip().upper()
            if not text or text.endswith(".BJ"):
                continue
            wanted.append(text)
        if not wanted:
            return []

        client = self._get_client()
        records: list[dict] = []
        total = len(wanted)
        done = 0
        records_since = 0  # 已扫描行数, 用于区分「整页识别失败」与「个别行异常」
        complete = True   # 每批都成功扫描完才为 True; 任一 break 都意味着数据不完整
        for batch in pingan_client.chunked(wanted, pingan_client.MAX_CODES_PER_CALL):
            try:
                # 内部 `159682.SZ` → 平安 `SZ159682`; 直接透传会被整批判为 not_found
                rows, server_ts = client.quote([to_pa_symbol(s) for s in batch])
            except PingAnRateLimitError as e:
                logger.warning("平安 ETF 快照限流, 本轮保留已有数据: %s", e)
                complete = False
                break
            except PingAnError as e:
                logger.warning("平安 ETF 快照拉取失败, 本轮保留已有数据: %s", e)
                complete = False
                break
            fetched_ms = server_ts or int(time.time() * 1000)
            missing = 0
            for row in rows:
                rec = _map_quote_row(row, fetched_ms)
                if rec is not None:
                    records.append(rec)
                else:
                    missing += 1
            # 整页都识别不出 code → 大概率接口 schema 变了, 明确告警而非静默空数据
            if missing and not records_since:
                logger.warning("平安快照本批 %d 行全部缺少可识别 code 字段, 疑似接口结构变化", missing)
            records_since += len(rows)
            done += len(batch)
            if on_chunk_done is not None:
                on_chunk_done(done, total)
        # BUG-C: 把「是否完整」暴露给上层。限流后的半截数据绝不能被当作完整成功
        # 缓存 60s 并标记整分区覆盖 —— 那样未补到的 ETF 会被静默丢弃。
        self.last_etf_fetch_complete = complete
        if records:
            logger.info(
                "平安 ETF 快照完成: %d 条 (%d 只请求, %s)",
                len(records), total, "完整" if complete else "部分/限流",
            )
        return records

    def get_realtime(self) -> list[dict]:
        """全市场快照契约位: 本源不提供全市场, 显式返回空并留痕。

        声明等价的软失败语义 (空列表 + warning), 让误路由到本源的 realtime
        能力直接可见「没有数据」, 而不是静默空轮。
        """
        logger.warning("平安源不提供全市场实时快照; 请把 realtime 能力路由到其他数据源")
        return []

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页「试拉」: 返回 {provider, dataset, rows, columns, preview, error?}。"""
        if dataset != "etf":
            return {
                "provider": self.name, "dataset": dataset, "rows": 0,
                "columns": [], "preview": [],
                "error": "平安源只提供 ETF 实时快照; 该能力会回退默认数据源",
            }
        probe = list(symbols or [])[: pingan_client.MAX_CODES_PER_CALL] or ["159682.SZ"]
        try:
            records = self.get_etf_realtime(probe)
        except ProviderRequestError as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0,
                    "columns": [], "preview": [], "error": str(e)}
        if not records:
            return {"provider": self.name, "dataset": dataset, "rows": 0,
                    "columns": [], "preview": [],
                    "error": "无返回数据 (检查 API Key、额度和标的代码)"}
        frame = pl.DataFrame(records)
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": frame.height,
            "columns": frame.columns,
            "preview": frame.head(5).to_dicts(),
        }


def today_cn() -> date:
    """当日 (北京)。仅用于试拉展示, 不参与落盘日期归属。"""
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone(timedelta(hours=8))).date()
