"""ETF 估值分位 — 平安金库 `ETF_FILTER` 的落地层。

为什么需要这一层:
  TSP 的 `ext_valuation` 只有个股「当天快照」, 没有历史序列, 也就没有分位
  (见 `docs/数据缺口清单与优先级.md` 缺口 1)。平安金库的 ETF_FILTER 直接给出
  `peFenWei`/`pbFenWei` 分位, 还附带费率/股息/ROE/RSI/规模/跟踪指数, 是
  现成可用的估值维度来源。

接口形态 (实测 2026-11, 与行情接口完全不同):
  POST {BASE}/restapi/jinku/omm/v2/http/mop/vault/query
  headers: X-API-Key / X-Skill-ID: etf-filter / X-Request-ID / requestId / channelId
  body:    {"key": "ETF_FILTER", "wheres": [...], "fields": "...", "pageSize": 100}
  信封:    {responseCode, currentDate, total, rows}   ← 没有 data 层

关键约束 (决定这里的实现方式, 改动前先读):
  1. **没有页码参数**。pageSize 上限 100, 全市场 1721 只, 只能靠字段游标翻页。
  2. **scope / turnVolume20 不可筛选** —— 任何操作符都返回 total=0, 所以不能
     用「规模 < 上一页最小值」翻页; 能用游标的是 `code` (type 3 大于)。
     实测 `orderBy=code, orderType=A` + `wheres=[{code, 3, 上一页末个}]`
     18 页拉全 1721 只, 每页 100 条 (末页 38 条) —— 见 walk_universe。
  3. **type 8 (数组集合) 被服务端静默忽略**, 传 code 数组只会拿到默认列表。
     逐只查询必须 `type: 5` 等值。
  4. 数值全是字符串且单位混杂: 分位/涨跌幅/ROE/费率是小数 (0.0015 = 0.15%),
     `yielD`/`foldingPremiunRates`/`scopeIncreaseM` 已经是百分数。
     本模块统一换算成百分数存放, 只有 pe/pb/rsi 保持原始比值。
  5. 额度 2000 次/日。全市场一轮 18 次, 可以每天跑。

接入方式: 内置扩展表 `ext_etf_valuation` (timeseries)。拉取不像普通扩展表那样
  是「单请求 + 字段映射」, 而是「游标翻页 + 逐字段归一」, 所以挂到
  `PullConfig.adapter` (配置即配方, 与 fanout 同思路) 上, 由
  `ext_pull._fetch_adapter_rows` 分发到本模块的 `fetch_rows`。写入/分区/派生
  失效/API/前端仍走通用链, 零特例。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path

from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

CONFIG_ID = "ext_etf_valuation"
ADAPTER_KIND = "pingan_etf_valuation"
# 独立播种标记: 概念/行业预设的 .builtin_presets_seeded 早已落盘, 复用会让新预设
# 在老库上永远不创建 (「只播种一次」契约)。
_SEED_MARKER = ".etf_valuation_seeded"

# pageSize 服务端硬上限; 全市场 1721 只 → 18 页, 留 2 倍余量给标的扩张
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 40
# 并发上限 (用户约束): 游标翻页本质串行, 这里只约束将来可能加的并行支路。
MAX_CONCURRENCY = 4

REQUEST_TIMEOUT_S = 60.0

# 请求字段: 一次要全, 免得为补字段再翻 18 页。
# scope / turnVolume20 不能筛选但能返回, 保留用于展示与本地筛选。
REQUEST_FIELDS = (
    "code,etfName,fundCompany,fundType1,fundType2,indexName,indexCode,"
    "scope,cost,pe,pb,peFenWei,pbFenWei,roe,rsi,turnVolume20,yielD,"
    "foldingPremiunRates,dayRise5,dayRise20,dayRise60,dayRise120,"
    "thisYearRise,oneYearRise,scopeIncreaseM,favoriteNum,search1W"
)

# 筛选参数 → (归一列, 比较方式)。**全部在本地对归一后的列做**, 不下发服务端。
#
# 为什么不透传服务端 wheres (踩过的坑):
#   源把数值都以字符串返回, 服务端的比较/排序按字符串语义走 —— 实测
#   `orderBy=pe, orderType=A` 的首条是 10.08 而不是 6.48 (即 "1x" 排在 "6x" 前),
#   `pe < 20` 服务端算 676 只而本地算 717 只, 且 `pe<20 & fundType1=规模宽基`
#   服务端 201 只 > 本地 180 只 (服务端筛选用值与返回字段值不是同一口径)。
#   分位/费率/股息这类看起来对得上只是巧合 (同为 0.x 位数相同)。
#   结论: 数值比较一律在本地做, 服务端只负责「按 code 游标翻页」这一件事。
SCREEN_FILTERS: dict[str, tuple[str, str]] = {
    "fund_type1": ("fund_type1", "in"),         # 逗号分隔多值, 任一命中
    "fund_type2": ("fund_type2", "contains"),
    "index_name": ("index_name", "contains"),
    "code": ("symbol", "symbol"),
    "pe_max": ("pe", "<="),
    "pe_min": ("pe", ">="),
    "pb_max": ("pb", "<="),
    "pb_min": ("pb", ">="),
    "pe_percentile_max": ("pe_percentile", "<="),
    "pb_percentile_max": ("pb_percentile", "<="),
    "dividend_yield_min": ("dividend_yield", ">="),
    "cost_max": ("cost", "<="),
    "rsi_max": ("rsi", "<="),
    "rsi_min": ("rsi", ">="),
    "roe_min": ("roe", ">="),
    # 服务端筛不了 (任何操作符 total=0), 本地能筛 —— 这才是它们的正确用法
    "scope_min": ("scope", ">="),
    "amount_20d_min": ("amount_20d", ">="),
}

# ---------------------------------------------------------------------------
# 字段归一: 源字段 → 扩展表列名
# ---------------------------------------------------------------------------

_TEXT_FIELDS = (
    ("etfName", "name"),
    ("fundCompany", "fund_company"),
    ("fundType1", "fund_type1"),
    ("fundType2", "fund_type2"),
    ("indexName", "index_name"),
    ("indexCode", "index_code"),
)
# 源为小数, 统一 ×100 存百分数 (0.0015 → 0.15)
_PCT_FROM_DECIMAL = (
    ("peFenWei", "pe_percentile"),
    ("pbFenWei", "pb_percentile"),
    ("roe", "roe"),
    ("cost", "cost"),
    ("dayRise5", "rise_5d"),
    ("dayRise20", "rise_20d"),
    ("dayRise60", "rise_60d"),
    ("dayRise120", "rise_120d"),
    ("thisYearRise", "rise_ytd"),
    ("oneYearRise", "rise_1y"),
)
# 源已是百分数, 原样存 (-1.58 表示 -1.58%)
_PCT_DIRECT = (
    ("yielD", "dividend_yield"),
    ("foldingPremiunRates", "premium_rate"),
    ("scopeIncreaseM", "scope_growth_m"),
)
_PLAIN_NUM = (
    ("pe", "pe"),
    ("pb", "pb"),
    ("rsi", "rsi"),
    ("scope", "scope"),
    ("turnVolume20", "amount_20d"),
    ("favoriteNum", "favorite_num"),
    ("search1W", "search_1w"),
)

# (列名, 类型, 中文名) —— 单位写进中文名, 避免下游猜口径
VALUATION_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("name", "string", "基金简称"),
    ("fund_company", "string", "基金公司"),
    ("fund_type1", "string", "一级分类"),
    ("fund_type2", "string", "二级分类"),
    ("index_name", "string", "跟踪指数"),
    ("index_code", "string", "跟踪指数代码"),
    ("scope", "float", "规模(元)"),
    ("cost", "float", "费率(%)"),
    ("pe", "float", "PE"),
    ("pb", "float", "PB"),
    ("pe_percentile", "float", "PE分位(%)"),
    ("pb_percentile", "float", "PB分位(%)"),
    ("roe", "float", "ROE(%)"),
    ("rsi", "float", "RSI(6)"),
    ("amount_20d", "float", "20日成交额(元)"),
    ("dividend_yield", "float", "股息率(%)"),
    ("premium_rate", "float", "折溢价率(%)"),
    ("rise_5d", "float", "近5日涨幅(%)"),
    ("rise_20d", "float", "近20日涨幅(%)"),
    ("rise_60d", "float", "近60日涨幅(%)"),
    ("rise_120d", "float", "近120日涨幅(%)"),
    ("rise_ytd", "float", "今年以来涨幅(%)"),
    ("rise_1y", "float", "近一年涨幅(%)"),
    ("scope_growth_m", "float", "近1月规模增幅(%)"),
    ("favorite_num", "float", "自选人数"),
    ("search_1w", "float", "近1周搜索人数"),
    ("source_date", "string", "数据日期"),
)

_NULL_TEXT = {"", "none", "null", "nan", "-", "--"}

# 可排序键 = 归一后的全部列名 (含 symbol/code); 源的驼峰名走下面的别名表。
SCREEN_ORDER_BY = frozenset({"symbol", "code"} | {name for name, _, _ in VALUATION_FIELDS})

# 排序别名: 源字段名 (peFenWei/yielD/etfName) → 本表列名。两种写法都收。
SCREEN_ORDER_BY_ALIASES: dict[str, str] = {
    "etfName": "name",
    "fundCompany": "fund_company",
    "fundType1": "fund_type1",
    "fundType2": "fund_type2",
    "indexName": "index_name",
    "indexCode": "index_code",
    "peFenWei": "pe_percentile",
    "pbFenWei": "pb_percentile",
    "yielD": "dividend_yield",
    "foldingPremiunRates": "premium_rate",
    "dayRise5": "rise_5d",
    "dayRise20": "rise_20d",
    "dayRise60": "rise_60d",
    "dayRise120": "rise_120d",
    "thisYearRise": "rise_ytd",
    "oneYearRise": "rise_1y",
    "scopeIncreaseM": "scope_growth_m",
    "turnVolume20": "amount_20d",
    "favoriteNum": "favorite_num",
    "search1W": "search_1w",
}


def _num(value) -> float | None:
    """宽松取数: 字符串/数字 → float, 剔除 % 与千分位; 空值/占位符 → None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").rstrip("%")
    if text.casefold() in _NULL_TEXT:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _text(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.casefold() in _NULL_TEXT else text


def pa_to_symbol(code: str) -> str | None:
    """平安代码 `SH510300` → 内部 `510300.SH`。无法识别返回 None。"""
    raw = _text(code).upper()
    if len(raw) != 8 or raw[:2] not in ("SH", "SZ"):
        return None
    digits = raw[2:]
    if not digits.isdigit():
        return None
    return f"{digits}.{raw[:2]}"


def to_ext_row(raw: dict, source_date: str = "") -> dict | None:
    """平安一行 → 扩展表一行。代码无法识别时返回 None (整行丢弃)。"""
    symbol = pa_to_symbol(raw.get("code") or "")
    if symbol is None:
        return None
    row: dict = {
        "symbol": symbol,
        "code": symbol.split(".")[0],
        "source_date": source_date,
    }
    for src, dst in _TEXT_FIELDS:
        row[dst] = _text(raw.get(src))
    for src, dst in _PLAIN_NUM:
        row[dst] = _num(raw.get(src))
    for src, dst in _PCT_DIRECT:
        row[dst] = _num(raw.get(src))
    for src, dst in _PCT_FROM_DECIMAL:
        value = _num(raw.get(src))
        row[dst] = None if value is None else round(value * 100.0, 6)
    return row


# ---------------------------------------------------------------------------
# 拉取: code 游标翻页
# ---------------------------------------------------------------------------

def walk_universe(
    client,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> tuple[list[dict], str]:
    """按 `code` 游标翻完全市场 ETF, 返回 (归一后的行, 数据日期)。

    游标语义: `wheres=[{code, type:3, value: 上一页末个 code}]` + `orderBy=code A`。
    相邻页会重复末个 code (服务端不排除边界), 用 code 去重保证不重复落库。
    翻页终止: 空页 / 短页 / 游标不再前进 / 达到 max_pages。
    """
    rows: list[dict] = []
    seen: set[str] = set()
    cursor = ""
    source_date = ""
    pages = 0
    for page in range(1, max_pages + 1):
        wheres = [{"field": "code", "type": 3, "value": cursor}] if cursor else []
        page_rows, _total, page_date = client.query_etf_filter({
            "wheres": wheres,
            "fields": REQUEST_FIELDS,
            "orderBy": "code",
            "orderType": "A",
            "pageSize": page_size,
        })
        pages = page
        source_date = page_date or source_date
        if not page_rows:
            break
        for raw in page_rows:
            code = _text(raw.get("code") or "").upper()
            if not code or code in seen:
                continue
            row = to_ext_row(raw, source_date)
            if row is None:
                seen.add(code)   # 已处理过, 避免同一无效代码反复进入
                continue
            seen.add(code)
            rows.append(row)
        codes = [_text(r.get("code") or "").upper() for r in page_rows if r.get("code")]
        if len(page_rows) < page_size:
            break                      # 短页 = 最后一页
        if not codes or codes[-1] <= cursor:
            logger.warning(
                "ETF 估值游标未前进 (cursor=%s, last=%s), 提前结束以免死循环",
                cursor, codes[-1] if codes else "",
            )
            break
        cursor = codes[-1]

    if pages >= max_pages:
        logger.warning(
            "ETF 估值拉取达到 max_pages=%d 上限, 结果可能不完整 (%d 行)", max_pages, len(rows)
        )
    logger.info("ETF 估值: %d 页拉取 %d 只 (数据日期 %s)", pages, len(rows), source_date or "-")
    return rows, source_date


async def fetch_rows(config: ExtConfig, target_date: date, data_dir=None) -> list[dict]:
    """`pull.adapter.kind = pingan_etf_valuation` 的适配器入口。

    与通用拉取的区别: 通用路径是单请求 + 字段映射, 这里是游标翻页 + 逐字段
    归一。返回的行已含 symbol/code, 后续写入链 (分区/去重/派生失效) 零改动。
    """
    from app.plugins.pingan.provider import get_api_key

    adapter = (config.pull.adapter if config.pull else None) or {}
    page_size = int(adapter.get("page_size") or DEFAULT_PAGE_SIZE)
    page_size = max(1, min(page_size, 100))
    max_pages = int(adapter.get("max_pages") or DEFAULT_MAX_PAGES)

    api_key = get_api_key()
    if not api_key:
        raise ValueError(
            "未配置平安 API Key — 请在 设置 → 数据源 → 平安证券 填写, "
            "或配置环境变量 PINGAN_SKILL_APIKEY"
        )
    from app.plugins.pingan.client import PingAnClient

    client = PingAnClient(api_key=api_key, timeout=REQUEST_TIMEOUT_S)
    # urllib 是同步的, 放进线程避免堵事件循环; 游标翻页本身就是串行, 天然不超
    # MAX_CONCURRENCY 并发上限。
    rows, _source_date = await asyncio.to_thread(
        walk_universe, client, page_size=page_size, max_pages=max_pages
    )
    return rows


# ---------------------------------------------------------------------------
# 即时筛选 (本地对归一后的列做, 服务端只负责翻页)
# ---------------------------------------------------------------------------

# 全市场一轮 18 次请求 / 约 8 秒。筛选是「看一眼」的操作, 缓存几分钟足以,
# 避免每点一次筛就重翻 18 页 (额度 2000/日)。
_UNIVERSE_TTL_S = 180.0
_UNIVERSE_CACHE: dict[str, object] = {}


def clear_universe_cache() -> None:
    """丢弃进程内缓存 (测试与「立即刷新」用)。"""
    _UNIVERSE_CACHE.clear()


def _load_universe(
    *, page_size: int = DEFAULT_PAGE_SIZE, max_pages: int = DEFAULT_MAX_PAGES
) -> tuple[list[dict], str, bool]:
    """取全市场归一后的行: (rows, 数据日期, 是否命中缓存)。"""
    import time

    from app.plugins.pingan.client import PingAnClient
    from app.plugins.pingan.provider import get_api_key

    cached_at = _UNIVERSE_CACHE.get("at")
    if cached_at is not None and time.monotonic() - float(cached_at) < _UNIVERSE_TTL_S:
        return list(_UNIVERSE_CACHE["rows"]), str(_UNIVERSE_CACHE["date"]), True

    api_key = get_api_key()
    if not api_key:
        raise ValueError("未配置平安 API Key — 请在 设置 → 数据源 → 平安证券 填写")
    client = PingAnClient(api_key=api_key, timeout=REQUEST_TIMEOUT_S)
    # walk_universe 返回的已经过 to_ext_row 归一, 不能再归一一次 (二次归一会把
    # 已是内部格式的 code 丢掉)。
    rows, source_date = walk_universe(
        client, page_size=page_size, max_pages=max_pages
    )
    _UNIVERSE_CACHE.update(rows=rows, date=source_date, at=time.monotonic())
    return list(rows), source_date, False


def _filter_code(value: object) -> str:
    """`510300` / `SH510300` / `510300.SH` → 内部 `510300.SH`。"""
    text = _text(value).upper()
    if len(text) == 9 and text[6] == "." and text[:6].isdigit():
        suffix = text[7:]
        if suffix in ("SH", "SZ"):
            return f"{text[:6]}.{suffix}"
    symbol = pa_to_symbol(text)
    if symbol is not None:
        return symbol
    if len(text) == 6 and text.isdigit():
        return pa_to_symbol(("SH" if text[0] in "5689" else "SZ") + text) or ""
    raise ValueError(f"代码格式无法识别: {value!r} (支持 510300 / SH510300 / 510300.SH)")


def apply_filters(rows: list[dict], params: dict[str, object]) -> list[dict]:
    """按 SCREEN_FILTERS 本地过滤。未知键/非数值一律抛 ValueError (fail-closed)。

    空值语义: 列值为 None 的行**不满足**任何数值/文本条件 (不会被当成 0),
    这样「PE 分位 ≤ 30%」不会把无 PE 的 QDII 混进来。
    """
    criteria: list[tuple[str, str, object]] = []
    for name, value in params.items():
        if value is None or value == "":
            continue
        if name not in SCREEN_FILTERS:
            raise ValueError(f"不支持的筛选参数: {name}")
        column, op = SCREEN_FILTERS[name]
        if op == "in":
            wanted = [
                x.strip()
                for x in str(value).replace("，", ",").split(",")
                if x.strip()
            ]
            if not wanted:
                raise ValueError(f"筛选项 {name} 需要文本: {value!r}")
            criteria.append((column, op, wanted))
        elif op == "contains":
            text = _text(value)
            if not text:
                raise ValueError(f"筛选项 {name} 需要文本: {value!r}")
            criteria.append((column, op, text))
        elif op == "symbol":
            criteria.append((column, "==", _filter_code(value)))
        else:
            number = _num(value)
            if number is None:
                raise ValueError(f"筛选项 {name} 需要数值: {value!r}")
            criteria.append((column, op, number))

    if not criteria:
        return list(rows)

    matched: list[dict] = []
    for row in rows:
        keep = True
        for column, op, rhs in criteria:
            current = row.get(column)
            if op in ("in", "contains", "=="):
                if op == "in":
                    ok = current in rhs  # type: ignore[operator]
                elif op == "contains":
                    ok = isinstance(current, str) and rhs in current  # type: ignore[operator]
                else:
                    ok = current == rhs
            else:
                ok = current is not None and (
                    current <= rhs if op == "<=" else current >= rhs  # type: ignore[operator]
                )
            if not ok:
                keep = False
                break
        if keep:
            matched.append(row)
    return matched


def _sort_rows(rows: list[dict], order_by: str, descending: bool) -> list[dict]:
    """按列排序, 空值恒定排在最后 (降序也不会把空值顶到前面)。"""
    present = [r for r in rows if r.get(order_by) is not None]
    empty = [r for r in rows if r.get(order_by) is None]
    present.sort(key=lambda r: r[order_by], reverse=descending)
    return present + empty


def screen(
    params: dict[str, object],
    *,
    order_by: str = "pe_percentile",
    descending: bool = False,
    limit: int = 50,
) -> dict:
    """筛选全市场 ETF (本地过滤/排序, 绑定全市场数据 3 分钟)。"""
    order_by = SCREEN_ORDER_BY_ALIASES.get(order_by, order_by)
    if order_by not in SCREEN_ORDER_BY:
        raise ValueError(
            f"不支持的排序字段: {order_by} (可用: {', '.join(sorted(SCREEN_ORDER_BY))})"
        )
    limit = max(1, min(int(limit), 100))

    rows, source_date, cached = _load_universe()
    matched = apply_filters(rows, params)
    page = _sort_rows(matched, order_by, descending)[:limit]
    return {
        "total": len(matched),
        "returned": len(page),
        "source_date": source_date,
        "cached": cached,
        "order_by": order_by,
        "descending": descending,
        "items": page,
    }


# ---------------------------------------------------------------------------
# 内置预设 (出厂即带完整可跑的配方)
# ---------------------------------------------------------------------------

def preset() -> ExtConfig:
    """内置扩展表 `ext_etf_valuation`: 全市场 ETF 估值分位, 每交易日收盘后落库。"""
    from app.plugins.pingan.client import BASE_URL, ETF_FILTER_PATH

    return ExtConfig(
        id=CONFIG_ID,
        label="ETF 估值分位",
        mode="timeseries",
        fields=[ExtField(name, dtype, label) for name, dtype, label in VALUATION_FIELDS],
        description=(
            "全市场 ETF 估值/分位/费率/股息/规模 (平安金库 ETF_FILTER)。"
            "pe_percentile 等分位列单位是百分数 (0.15 表示 0.15%)。"
            "拉取配方见「拉取」页: code 游标翻页 18 次覆盖全市场。"
        ),
        symbol_map={},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=f"{BASE_URL}/{ETF_FILTER_PATH}",
            method="POST",
            response_path="rows",
            # 凭据复用行情插件同一把 Key (secrets.json: pingan_api_key)
            auth={"type": "header", "header": "X-API-Key", "key_ref": "pingan_api_key"},
            timeout_seconds=int(REQUEST_TIMEOUT_S),
            # 收盘后跑: 分位/规模当日定型, 且避开行情插件盘中轮询
            schedule_time="17:20",
            schedule_period="daily",
            schedule_trading_only=True,
            enabled=True,
            adapter={
                "kind": ADAPTER_KIND,
                "page_size": DEFAULT_PAGE_SIZE,
                "max_pages": DEFAULT_MAX_PAGES,
            },
        ),
    )


def get_preset(config_id: str) -> ExtConfig | None:
    return preset() if config_id == CONFIG_ID else None


async def ensure_preset(data_dir: Path) -> None:
    """启动时播种内置 ETF 估值表 (独立标记, 只播一次)。

    与 ext_presets 同纪律: 缺标记才建; 已存在则只在「出厂空壳」时补 pull,
    绝不覆盖用户自定义配置; 失败只记 warning 不阻断启动。
    """
    store = ExtConfigStore(data_dir)
    marker = data_dir / "ext_data" / _SEED_MARKER
    first_install = not marker.exists()
    config = preset()
    try:
        existing = store.get(CONFIG_ID)
        if existing is None:
            if first_install:
                store.upsert(config)
                logger.info("内置扩展表 %s 已就绪 (含出厂拉取配方)", CONFIG_ID)
        elif _is_factory_shell(existing.pull):
            existing.pull = config.pull
            store.upsert(existing)
            logger.info("内置扩展表 %s 出厂配方已补齐", CONFIG_ID)
    except Exception as e:  # noqa: BLE001 — 启动契约: 预设写入失败不阻断
        logger.warning("内置扩展表 %s 配置写入失败 (不影响启动): %s", CONFIG_ID, e)
    if first_install:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(marker, "")
        except Exception as e:  # noqa: BLE001
            logger.warning("ETF 估值播种标记写入失败 (下次启动会重试): %s", e)


def _is_factory_shell(pull: PullConfig | None) -> bool:
    """现有 pull 是否为空壳 (无 URL / 无 adapter) —— 需要补出厂配方。"""
    if pull is None:
        return True
    if not (pull.url or "").strip():
        return True
    return not pull.adapter
