"""热股风向标 (平安 skill=hot-stock) → 四张扩展表 + 即时榜单 API。

补的是 TSP 原本完全没有的一个维度: **关注度**。行情、资金流、两融、龙虎榜都只回答
「发生了什么」, 榜单回答「散户正在看什么」—— 热搜(主动找)、加自选(持续跟踪)、
浏览(看一眼)、飙升(变化速度), 四种行为深度。热度值 `cnt` 逐日累积后可以算增速,
这是 `ext_fundflow` 之类给不了的。

## 接口实测 (2026-11-19)

- POST `restapi/hotstock/isspContentService/hotStock/list`, skill id `hot-stock`。
- 信封 `{status, errmsg, results}` —— **成功值是 `status == 1`**, 既不是行情的
  `code == 0` 也不是金库的 `responseCode == "0"`。第三套约定。
- `results = {list, page, size, total, updateTime}`; `total` 恒为 100 (榜单深度上限)。
- body: `module_code` (4 榜) + `type_code` (5 类) + `time_range` (**整数** 1=1h / 2=24h)
  + `size` (1..100) + 可选 `page`。
- 非法 `time_range`/`module_code` **不报错**: 返回 `status=1, total=0, list=[]`。
  空榜单与参数错误在协议上不可区分 → 取值校验必须前置。
- 条目字段: `marketType` / `stockCode` / `name` / `chg`(小数字符串, 需 ×100) /
  `cnt`(热度值) / `calcDate`(2026111906) / `tagList[{name,type,url}]` /
  `news{title,newsUrl}` / `updateTime`("2026-11-19 06:00:00")。
- `type_code=block` 的 `marketType` 是 `BI`, 代码是板块号 (994247), 不是可交易标的。

## 为什么四张表而不是一张

扩展表的去重键是 `[symbol]` (+ 时间列) —— 见 `ext_data.write_ext_parquet`。同一只票
在四个榜单里各出现一次, 塞进一张表会被去重成一行。拆成 `ext_hot_stock` /
`ext_hot_select` / `ext_hot_watch` / `ext_hot_surge` 四张表, 每张表内 symbol 每日唯一,
跨榜对比靠 symbol+date join。

四张表各拉 aStock + etf 两类 (block/hkStock/bond 对 TSP 无补充价值: 板块已有
`ext_gn_ths`/`ext_hy_ths`), 每表每日 2 次请求, 合计 8 次。
"""
from __future__ import annotations

import logging
from datetime import date

from app.services.ext_data import (
    PLATE_TS_FIELD,
    ExtConfig,
    ExtConfigStore,
    ExtField,
    PullConfig,
)

logger = logging.getLogger(__name__)

# 榜单类型 → (表 id, 中文名)。module_code 直接用平安的取值, 减少一层映射。
BOARDS: dict[str, tuple[str, str]] = {
    "hot_stock": ("ext_hot_stock", "热搜榜"),
    "hot_select": ("ext_hot_select", "加自选榜"),
    "hot_watch": ("ext_hot_watch", "浏览榜"),
    "hot_surge": ("ext_hot_surge", "飙升榜"),
}

# 二级类别 → 中文名。前两个是落库范围, 后三个只供即时 API 使用。
ASSET_KINDS: dict[str, str] = {
    "aStock": "A股",
    "etf": "ETF",
    "block": "板块概念",
    "hkStock": "港股",
    "bond": "可转债",
}
LANDED_ASSET_KINDS: tuple[str, ...] = ("aStock", "etf")

# 时间区间: 接口收整数, 对外收可读值。
TIME_RANGES: dict[int, str] = {1: "1小时", 2: "24小时"}
TIME_RANGE_ALIASES: dict[str, int] = {
    "1h": 1, "1": 1, "1小时": 1,
    "24h": 2, "24": 2, "1d": 2, "24小时": 2,
}
DEFAULT_TIME_RANGE = 2

MAX_SIZE = 100          # 服务端榜单深度上限
INTRADAY_INTERVAL_MIN = 30     # 盘中轮询间隔 (每 30 分钟一轮, 4 表 × 2 类 = 8 请求)
INTRADAY_WINDOW_START = "09:30"
INTRADAY_WINDOW_END = "15:00"
INTRADAY_TIME_FIELD = PLATE_TS_FIELD  # 注入拉取时刻: 每盘各存一行, 热度增量可算
DEFAULT_SIZE = 100
MAX_CONCURRENCY = 4     # 声明上限; 实际是串行 (见 collect_boards)

ADAPTER_KIND = "pingan_hot_stock"
_SEED_MARKER = ".hot_stock_seeded"
REQUEST_TIMEOUT_S = 60.0

# 表结构。`rank` 是榜单内序号, 热度值越高越靠前; 同一 symbol 在不同表里 rank 不同。
HOT_STOCK_FIELDS: list[tuple[str, str, str]] = [
    ("symbol", "str", "代码"),
    ("code", "str", "源代码"),
    ("name", "str", "名称"),
    ("market_type", "str", "市场"),
    ("asset_kind", "str", "类别"),
    ("rank", "int", "排名"),
    ("hot_value", "int", "热度值"),
    ("chg", "f64", "涨跌幅(%)"),
    ("tags", "str", "榜单标签"),
    ("news_title", "str", "关联资讯"),
    ("news_url", "str", "资讯链接"),
    ("calc_date", "str", "统计时点"),
    ("source_date", "str", "榜单日期"),
    ("updated_at", "str", "榜单更新时间"),
]

_NULL_TEXT = {"", "none", "null", "nan", "-", "--"}


# ---------------------------------------------------------------------------
# 参数校验 (协议不报错, 只能自己拦)
# ---------------------------------------------------------------------------

def normalize_board(value: object) -> str:
    board = str(value or "").strip()
    if board not in BOARDS:
        raise ValueError(
            f"不支持的榜单: {value!r} (可用: {', '.join(BOARDS)})"
        )
    return board


def normalize_asset_kind(value: object) -> str:
    kind = str(value or "").strip()
    if kind not in ASSET_KINDS:
        raise ValueError(
            f"不支持的类别: {value!r} (可用: {', '.join(ASSET_KINDS)})"
        )
    return kind


def normalize_time_range(value: object) -> int:
    """收 1/2 或 "1h"/"24h"/"24小时" 等可读值, 统一成接口要的整数。"""
    if value is None or value == "":
        return DEFAULT_TIME_RANGE
    if isinstance(value, int) and value in TIME_RANGES:
        return value
    text = str(value).strip().lower()
    if text in TIME_RANGE_ALIASES:
        return TIME_RANGE_ALIASES[text]
    if text.isdigit() and int(text) in TIME_RANGES:
        return int(text)
    raise ValueError(
        f"不支持的时间区间: {value!r} (可用: 1/1h, 2/24h; 传别的值服务端会静默返回空榜单)"
    )


def normalize_size(value: object) -> int:
    if value is None or value == "":
        return DEFAULT_SIZE
    try:
        size = int(value)   # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise ValueError(f"size 需要整数: {value!r}") from e
    if not 1 <= size <= MAX_SIZE:
        raise ValueError(f"size 必须在 1..{MAX_SIZE}: {size}")
    return size


# ---------------------------------------------------------------------------
# 行归一
# ---------------------------------------------------------------------------

def _text(value: object) -> str:
    text = str(value if value is not None else "").strip()
    return "" if text.lower() in _NULL_TEXT else text


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value).replace("%", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int(value: object) -> int | None:
    number = _num(value)
    return None if number is None else round(number)


def pa_symbol(market_type: object, code: object) -> str | None:
    """`SZ` + `002242` → `002242.SZ`; 板块 (`BI`) 也给一个稳定标识 `994247.BI`。"""
    code_text = _text(code)
    market = _text(market_type).upper()
    if not code_text:
        return None
    if market in ("SH", "SZ", "BJ", "HK", "BI"):
        return f"{code_text}.{market}"
    return None


def _tags(raw: object) -> str:
    if not isinstance(raw, list):
        return ""
    names: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            name = _text(item.get("name"))
            if name and name not in names:
                names.append(name)
    return "|".join(names)


def _news(raw: object) -> tuple[str, str]:
    if not isinstance(raw, dict):
        return "", ""
    return _text(raw.get("title")), _text(raw.get("newsUrl"))


def _date_of(updated_at: str) -> str:
    """`2026-11-19 06:00:00` → `2026-11-19`。"""
    text = _text(updated_at)
    return text[:10] if len(text) >= 10 else ""


def to_ext_row(raw: dict, asset_kind: str, rank: int, source_date: str = "") -> dict | None:
    """一条榜单条目 → 扩展表行。无法识别市场的条目返回 None (跳过该行)。"""
    if not isinstance(raw, dict):
        return None
    symbol = pa_symbol(raw.get("marketType"), raw.get("stockCode"))
    if symbol is None:
        return None
    updated_at = _text(raw.get("updateTime"))
    title, url = _news(raw.get("news"))
    chg = _num(raw.get("chg"))
    calc = _text(raw.get("calcDate"))
    return {
        "symbol": symbol,
        "code": _text(raw.get("stockCode")),
        "name": _text(raw.get("name")),
        "market_type": _text(raw.get("marketType")).upper(),
        "asset_kind": asset_kind,
        # chg 是小数 (-0.0999 = -9.99%), 与 TSP 其它涨跌幅列一样存百分数
        "chg": None if chg is None else round(chg * 100.0, 4),
        "rank": int(rank),
        "hot_value": _int(raw.get("cnt")),
        "tags": _tags(raw.get("tagList")),
        "news_title": title,
        "news_url": url,
        "calc_date": calc,
        "source_date": source_date or _date_of(updated_at),
        "updated_at": updated_at,
    }


def normalize_rows(
    raw_rows: list[dict], asset_kind: str, meta: dict | None = None
) -> list[dict]:
    """逐条归一 + 按热度值重排后的名次。"""
    source_date = _date_of(str((meta or {}).get("update_time") or ""))
    rows: list[dict] = []
    for index, raw in enumerate(raw_rows, start=1):
        row = to_ext_row(raw, asset_kind, index, source_date)
        if row is not None:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------

def fetch_board(
    client,
    board: str,
    asset_kind: str,
    *,
    time_range: int = DEFAULT_TIME_RANGE,
    size: int = DEFAULT_SIZE,
    page: int = 1,
) -> tuple[list[dict], dict]:
    """拉一个 (榜单, 类别) 组合, 返回 (归一后的行, 元信息)。"""
    board = normalize_board(board)
    asset_kind = normalize_asset_kind(asset_kind)
    time_range = normalize_time_range(time_range)
    size = normalize_size(size)
    payload = {
        "module_code": board,
        "type_code": asset_kind,
        "time_range": time_range,
        "size": size,
    }
    if page and int(page) > 1:
        payload["page"] = int(page)
    raw_rows, meta = client.hot_stock_list(payload)
    return normalize_rows(raw_rows, asset_kind, meta), meta


def collect_boards(
    client,
    board: str,
    *,
    asset_kinds: tuple[str, ...] = LANDED_ASSET_KINDS,
    time_range: int = DEFAULT_TIME_RANGE,
    size: int = DEFAULT_SIZE,
) -> tuple[list[dict], str]:
    """一个榜单拉多个类别 (串行, 天然不超并发上限), 返回 (行, 榜单日期)。

    某类失败只记 warning 不拖垮整张表 —— 8 个组合里挂一个不该让当天全空。
    """
    rows: list[dict] = []
    source_date = ""
    for kind in asset_kinds:
        try:
            part, meta = fetch_board(
                client, board, kind, time_range=time_range, size=size
            )
        except Exception as e:   # noqa: BLE001 — 单类失败不影响其它类
            logger.warning("热股榜单 %s/%s 拉取失败: %s", board, kind, e)
            continue
        if not part:
            logger.warning("热股榜单 %s/%s 返回空列表 (参数或当日数据问题)", board, kind)
        rows.extend(part)
        source_date = source_date or _date_of(str(meta.get("update_time") or ""))
    return rows, source_date


def _adapter(config: ExtConfig) -> dict:
    return (config.pull.adapter if config.pull else None) or {}


def board_of_config(config: ExtConfig) -> str:
    """从 adapter 配方里取出这张表对应的榜单。"""
    board = str(_adapter(config).get("board") or "").strip()
    return normalize_board(board)


async def fetch_rows(config: ExtConfig, target_date: date, data_dir=None) -> list[dict]:
    """`pull.adapter.kind = pingan_hot_stock` 的入口: 只返回行, 写入仍走通用链。"""
    from app.plugins.pingan.client import PingAnClient
    from app.plugins.pingan.provider import get_api_key

    adapter = _adapter(config)
    board = normalize_board(adapter.get("board"))
    kinds_raw = adapter.get("asset_kinds") or list(LANDED_ASSET_KINDS)
    kinds = tuple(normalize_asset_kind(k) for k in kinds_raw)
    time_range = normalize_time_range(adapter.get("time_range") or DEFAULT_TIME_RANGE)
    size = normalize_size(adapter.get("size") or DEFAULT_SIZE)

    api_key = get_api_key()
    if not api_key:
        raise ValueError(
            "未配置平安 API Key — 请在 设置 → 数据源 → 平安证券 填写, "
            "或配置环境变量 PINGAN_SKILL_APIKEY"
        )
    client = PingAnClient(api_key=api_key, timeout=REQUEST_TIMEOUT_S)
    rows, source_date = collect_boards(
        client, board, asset_kinds=kinds, time_range=time_range, size=size
    )
    if not rows:
        raise ValueError(f"热股榜单 {board} 返回 0 行 (检查 API Key 与榜单参数)")
    logger.info("热股榜单 %s: %d 行 (榜单日期 %s)", board, len(rows), source_date or "-")
    return rows


def live(
    board: str,
    asset_kind: str,
    *,
    time_range: object = DEFAULT_TIME_RANGE,
    size: object = 50,
    page: int = 1,
) -> dict:
    """即时拉一个榜单组合 (不落库), 供 API 直接返回。"""
    from app.plugins.pingan.client import PingAnClient
    from app.plugins.pingan.provider import get_api_key

    board = normalize_board(board)
    asset_kind = normalize_asset_kind(asset_kind)
    time_range_int = normalize_time_range(time_range)
    size_int = normalize_size(size)

    api_key = get_api_key()
    if not api_key:
        raise ValueError("未配置平安 API Key — 请在 设置 → 数据源 → 平安证券 填写")
    client = PingAnClient(api_key=api_key, timeout=REQUEST_TIMEOUT_S)
    rows, meta = fetch_board(
        client, board, asset_kind,
        time_range=time_range_int, size=size_int, page=page,
    )
    return {
        "board": board,
        "board_label": BOARDS[board][1],
        "asset_kind": asset_kind,
        "asset_kind_label": ASSET_KINDS[asset_kind],
        "time_range": time_range_int,
        "time_range_label": TIME_RANGES[time_range_int],
        "size": size_int,
        "returned": len(rows),
        "update_time": meta.get("update_time") or "",
        "source_date": _date_of(str(meta.get("update_time") or "")),
        "items": rows,
    }


# ---------------------------------------------------------------------------
# 内置预设: 每个榜单一张表
# ---------------------------------------------------------------------------

def preset(config_id: str | None = None) -> ExtConfig:
    """内置扩展表: 一个榜单一张 timeseries 表。

    **盘中轮询**而不是收盘后单次落库: `schedule_minutes=30` 区间制 +
    `time_window 09:30-15:00`, 且 `time_field` 用保留名 `__pull_ts__` 让
    每次拉取各存一盘 —— 热度值只有当日多次快照才能算「增量」, 而增量
    (涨幅榜/热度跃升) 才是这维度唯一有价值的信号。落库 8 请求/轮 × 8 轮
    = 64 次/日, 在平安 2000/日额度内。
    """
    from app.plugins.pingan.client import BASE_URL, HOT_STOCK_PATH

    if config_id is None:
        config_id = BOARDS["hot_stock"][0]
    board = next((m for m, (cid, _) in BOARDS.items() if cid == config_id), None)
    if board is None:
        raise ValueError(f"未知的热股榜单表: {config_id!r}")
    _, label = BOARDS[board]
    return ExtConfig(
        id=config_id,
        label=f"热股·{label}",
        mode="timeseries",
        fields=[ExtField(name, dtype, label_) for name, dtype, label_ in HOT_STOCK_FIELDS],
        description=(
            f"平安热股风向标「{label}」, 覆盖 {ASSET_KINDS['aStock']} + {ASSET_KINDS['etf']}。"
            "hot_value 是榜单热度值 (同榜同窗口内可比, 不同榜不同窗口不可比); "
            "chg 是百分数; rank 是榜单内序位。盘中每 30 分钟一轮, 每盘各存一行 "
            "(拉取时刻列), 用于算热度增量。"
        ),
        symbol_map={},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=f"{BASE_URL}/{HOT_STOCK_PATH}",
            method="POST",
            response_path="results",
            auth={"type": "header", "header": "X-API-Key", "key_ref": "pingan_api_key"},
            timeout_seconds=REQUEST_TIMEOUT_S,
            schedule_minutes=INTRADAY_INTERVAL_MIN,
            time_window_start=INTRADAY_WINDOW_START,
            time_window_end=INTRADAY_WINDOW_END,
            schedule_trading_only=True,
            time_field=INTRADAY_TIME_FIELD,
            enabled=True,
            adapter={
                "kind": ADAPTER_KIND,
                "board": board,
                "asset_kinds": list(LANDED_ASSET_KINDS),
                "time_range": DEFAULT_TIME_RANGE,
                "size": DEFAULT_SIZE,
            },
        ),
    )


PRESET_IDS: tuple[str, ...] = tuple(cid for cid, _ in BOARDS.values())


def get_preset(config_id: str) -> ExtConfig | None:
    if config_id not in PRESET_IDS:
        return None
    return preset(config_id)


def _is_factory_shell(pull: PullConfig | None) -> bool:
    """用户没配置过 (无 url 或没有 adapter 配方) —— 这种空壳可以补配方。"""
    if pull is None:
        return True
    if not str(pull.url or "").strip():
        return True
    return not (pull.adapter and str(pull.adapter.get("kind") or "").strip())


def _needs_intraday_upgrade(pull: PullConfig | None) -> bool:
    """持仓的出厂配方是「收盘后单次落库」; 改成盘中轮询后这些配置要跟着升级。

    只认出厂形状 (schedule_time=17:10 + 无 time_field + 本表 adapter) —— 用户
    手工改过的间隔/时刻/时间列一律不动。
    """
    if pull is None:
        return False
    adapter = pull.adapter
    if not adapter or str(adapter.get("kind") or "").strip() != ADAPTER_KIND:
        return False
    if pull.time_field:
        # 已经配了时间列 (无论是本模块的 __pull_ts__ 还是用户自选的列) → 不动
        return False
    # 出厂形状 = 「17:10 单次 + 1440 分钟间隔」。两个都是默认值才算没被用户动过;
    # 任一被改过就整份不动 (只改一半会得到既非每日也非盘中的混合配方)。
    return (
        str(pull.schedule_time or "").strip() == "17:10"
        and pull.schedule_minutes == 1440
    )


async def ensure_preset(data_dir) -> None:
    """首次启动播种四张表; 之后只补空壳配方, 绝不覆盖用户改过的配置。"""
    from pathlib import Path

    data_dir = Path(data_dir)
    marker = data_dir / "ext_data" / _SEED_MARKER
    first_install = not marker.exists()
    store = ExtConfigStore(data_dir)

    for config_id in PRESET_IDS:
        try:
            existing = store.get(config_id)
            if existing is None:
                if not first_install:
                    continue          # 用户删过就尊重删除, 不复活
                store.upsert(preset(config_id))
                logger.info("热股榜单预设已播种: %s", config_id)
                continue
            if _is_factory_shell(existing.pull):
                fresh = preset(config_id)
                existing.pull = fresh.pull
                existing.mode = fresh.mode
                if not existing.fields:
                    existing.fields = fresh.fields
                store.upsert(existing)
                logger.info("热股榜单预设配方已补齐: %s", config_id)
                continue
            if _needs_intraday_upgrade(existing.pull):
                fresh = preset(config_id)
                upgraded = ["time_field", "schedule_minutes", "schedule_time",
                            "schedule_period", "time_window_start", "time_window_end"]
                for attr in upgraded:
                    setattr(existing.pull, attr, getattr(fresh.pull, attr))
                store.upsert(existing)
                logger.info(
                    "热股榜单已升级为盘中轮询 (%smin, 每盘各存一行): %s",
                    INTRADAY_INTERVAL_MIN, config_id,
                )
        except Exception as e:   # noqa: BLE001 — 播种失败不该让应用起不来
            logger.warning("热股榜单预设播种失败 %s: %s", config_id, e)

    if first_install:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError as e:
            logger.warning("无法写入热股榜单播种标记: %s", e)
