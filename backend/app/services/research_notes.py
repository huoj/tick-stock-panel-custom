"""研报正文落地 — 平安「研报检索」(research-report) 的落地层。

为什么需要这一层:
  TSP 现有的 `ext_research` 只有 `buy_cnt/add_cnt/neutral_cnt/reduce_cnt/
  sell_cnt` 五个计数 —— 知道「有几家给买入」, 不知道「券商到底说了什么」。
  平安研报库提供检索式召回 + **正文片段**, 正好把这层信息补上。

接口形态 (实测 2026-11, 与行情/金库/热股又是不同的一套):
  POST {BASE}/restapi/yanbao/e-server-transfer-service/dify/retrieval
  headers: X-API-Key / X-Skill-ID: research-report / requestId
           + **Authorization: Bearer <平台内置 token>**   ← 不加直接 HTTP 500
  body:    {"knowledge_id": "1_5_520_v2", "query": "...",
            "retrieval_setting": {"top_k": 1..50, "score_threshhold": 0..1}}
  信封:    {records: [...], msg}    ← HTTP 200 即成功, 没有业务码

关键约束 (决定这里的实现方式, 改动前先读):
  1. **`score_threshhold` 是服务端字段名 (双 h), 但服务端并不按它过滤** ——
     同一 query 传 0.05 与 0.6 返回完全相同的记录集 (实测 44 条一字不差)。
     阈值只能在本地做。字段名不许「修正」成 threshold。
  2. **一条 record 只有 content / metadata{path,description} / score / title**。
     没有机构、没有研报类型、**没有发布日期**。
  3. 同一篇研报被切成多个片段分别返回 (同 `metadata.path` 里的 `id=`)。
     不按文档去重, 一张表会被同一篇研报的 4~6 个片段挤满。
  4. **片段是骨架, 不是整篇**: 实测 27 篇去重文档里 30% 不到 200 字 (只有标题 +
     核心观点标题 + 股票代码), 中位 815 字。所以机构/评级/发布日期**抽不出来**,
     实测收紧规则后:
        机构 1/27 且是错的 (`基金代销与证券` ← 从「基金代销与证券业务」误匹配)
        评级 0/27
        可信发布日期 1/27 (`# 东方财富(300059.SZ)` 下一行的 `2026年10月25日`)
     另有 `2026-07-31` 这类是**正文里的数据时点**(「截至2026年7月31日尚未回购」),
     不是发布日期。因此本模块**不落机构/评级/发布日期三列** —— 与其给假数据,
     不如只给片段原文, 让用户点开 `path` 看全文。
  5. 检索形态: **用 6 位代码查询的召回质量最好** (代码 20 片段 / 15~19 篇全命中;
     名称只召回 6 篇; 名称+代码 9~12 篇)。见 `query_for`。
  6. `knowledge_id` 写错 → HTTP 500 (与真故障同码), 只能靠错误文案区分。
  7. 额度 2000 次/日; 自选+持仓量级几十, 每日几十次请求。

接入方式: 内置扩展表 `ext_research_note` (timeseries, 无 time_field)。
  `write_ext_parquet` 的去重键是 `[symbol]`, 所以**每个标每日只留一行 —— 当日
  命中最优的那篇**。多篇文档的浏览走 `GET /api/research-notes/search` 实时接口。
  拉取挂到 `PullConfig.adapter` (kind=`pingan_research_note`), 由
  `ext_pull._fetch_adapter_rows` 分发到本模块 `fetch_rows`; 写入/分区/API/前端
  仍走通用链。
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import polars as pl

from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

CONFIG_ID = "ext_research_note"
ADAPTER_KIND = "pingan_research_note"
# 独立播种标记: 复用 ext_presets 的 `.builtin_presets_seeded` 会因 first_install
# 已经是 False 而永远不建这张表。
_SEED_MARKER = ".research_note_seeded"

KNOWLEDGE_ID = "1_5_520_v2"
DEFAULT_TOP_K = 10
MAX_TOP_K = 50
DEFAULT_THRESHOLD = 0.3
# 自选 + 持仓的量级是几十; 设上限防止用户误配 adapter.symbols 把额度打光。
MAX_SYMBOLS = 200
# 声明式并发上限。实际取数是串行 (逐标的顺序请求), 既满足额度纪律, 也避免
# 触发 429 —— 平安这套接口 0.4s 间隔才稳。
MAX_CONCURRENCY = 4
REQUEST_TIMEOUT_S = 90.0
SCHEDULE_TIME = "17:30"
SNIPPET_CHARS = 400
TITLES_KEEP = 3
TITLE_MAX_CHARS = 60

FIELDS: tuple[tuple[str, str, str], ...] = (
    ("symbol", "str", "代码"),
    ("code", "str", "6位代码"),
    ("name", "str", "名称"),
    ("doc_count", "int", "命中篇数"),
    ("top_score", "float", "最高相关度"),
    ("doc_id", "str", "文档ID"),
    ("title", "str", "最优研报标题"),
    ("titles", "str", "当日标题概览"),
    ("snippet", "str", "正文片段"),
    ("source_date", "str", "拉取日期"),
)
FIELD_NAMES = tuple(name for name, _, _ in FIELDS)

_DOC_ID_RE = re.compile(r"[?&]id=(\d+)")


def doc_key(record: dict) -> str:
    """文档标识: 优先 metadata.path 里的 `id=`, 退化为标题。

    path 形如 `/profile_platform/knowledge-base/5/knowledge/document-detail?id=20697&kb_id=520`;
    偶尔 `id=` 为空 (返回 `id=&kb_id=`), 这时只能拿标题当键。
    """
    meta = record.get("metadata") or {}
    path = str(meta.get("path") or "")
    match = _DOC_ID_RE.search(path)
    if match and match.group(1):
        return match.group(1)
    title = str(record.get("title") or "").strip()
    return f"title:{title}" if title else ""


def is_real_doc_id(value: str) -> bool:
    """`title:` 前缀的键只是去重用的替身, 不是真的文档 id。"""
    return bool(value) and not value.startswith("title:")


def dedupe_docs(records: list[dict]) -> list[dict]:
    """按文档去重 (同文档多片段留最高分), 按 score 降序。

    返回的每条带 `doc_id` 与归一后的 `score`。没有 doc_key 的片段直接丢弃
    (既无 id 又无标题, 无法归因)。
    """
    best: dict[str, dict] = {}
    for record in records:
        key = doc_key(record)
        if not key:
            continue
        try:
            score = float(record.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        current = best.get(key)
        if current is None or score > float(current.get("score") or 0.0):
            item = dict(record)
            item["doc_id"] = key
            item["score"] = score
            best[key] = item
    return sorted(best.values(), key=lambda r: float(r.get("score") or 0.0), reverse=True)


def filter_by_threshold(docs: list[dict], threshold: float) -> list[dict]:
    """本地阈值过滤 (服务端不生效, 见模块 docstring 第 1 条)。"""
    return [d for d in docs if float(d.get("score") or 0.0) >= float(threshold)]


def make_snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """压缩空白后截断 —— 片段里大量 markdown 表格换行, 原样存又大又不便读。"""
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "…"


def _title_of(doc: dict) -> str:
    title = str(doc.get("title") or "").strip()
    if not title:
        return ""
    return title if len(title) <= TITLE_MAX_CHARS else title[:TITLE_MAX_CHARS].rstrip() + "…"


def make_titles(docs: list[dict], keep: int = TITLES_KEEP) -> str:
    """当日标题概览 (前 keep 篇, `|` 分隔)。

    单篇的标题常有省略号且不含公司名 (「成本与零售挤压，盈利能力承压」), 给一列
    标题概览能一眼看出「今天有谁在写、写了几篇」。标题里带 `|` 的换成 `｜`。
    """
    titles: list[str] = []
    for doc in docs[:keep]:
        title = _title_of(doc)
        if title:
            titles.append(title.replace("|", "｜"))
    return "|".join(titles)


def to_ext_row(symbol: str, name: str, docs: list[dict], source_date: str) -> dict:
    """把一个标的当日命中的文档列表压成一行 (最优那篇 + 统计 + 标题概览)。"""
    best = docs[0] if docs else {}
    content = str(best.get("content") or "")
    doc_id = str(best.get("doc_id") or "")
    return {
        "symbol": symbol,
        "code": (symbol.split(".")[0] if "." in symbol else symbol),
        "name": name,
        "doc_count": len(docs),
        "top_score": float(best.get("score") or 0.0) if best else None,
        "doc_id": doc_id if is_real_doc_id(doc_id) else "",
        "title": str(best.get("title") or ""),
        "titles": make_titles(docs),
        "snippet": make_snippet(content),
        "source_date": source_date,
    }


def doc_view(doc: dict) -> dict:
    """API 输出形态 (与落库列对齐, 额外带 path 便于回查原文)。"""
    doc_id = str(doc.get("doc_id") or doc_key(doc))
    return {
        "doc_id": doc_id if is_real_doc_id(doc_id) else "",
        "title": str(doc.get("title") or ""),
        "score": float(doc.get("score") or 0.0),
        "snippet": make_snippet(doc.get("content") or ""),
        "path": str((doc.get("metadata") or {}).get("path") or ""),
    }


# ---------------------------------------------------------------------------
# 标的集合
# ---------------------------------------------------------------------------

def query_for(symbol: str, name: str = "") -> str:
    """检索关键词。实测 6 位代码召回最好, 非 A 股 (港股等) 退化为名称。"""
    code = symbol.split(".")[0] if "." in symbol else symbol
    if re.fullmatch(r"\d{6}", code):
        return code
    return name or code


def name_lookup(data_dir: Path) -> dict[str, str]:
    """symbol → name。读 instruments 与 instruments_etf 两张维表 (ETF 只在后者里)。

    维表异常只丢名称, 不该阻断研报落地。
    """
    names: dict[str, str] = {}
    for relative in (
        "instruments/instruments.parquet",
        "instruments_etf/instruments_etf.parquet",
    ):
        path = data_dir / relative
        if not path.exists():
            continue
        try:
            df = pl.read_parquet(path, columns=["symbol", "name"])
            names.update(dict(zip(df["symbol"].to_list(), df["name"].to_list(), strict=True)))
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 %s 失败 (名称留空): %s", relative, e)
    return names


def tracked_symbols(data_dir: Path, override: list[str] | None = None) -> list[dict]:
    """要跟踪的标的 = adapter.symbols (若有) 或 自选 ∪ 持仓。

    返回 `[{symbol, name}]`, 按 symbol 去重排序, 上限 MAX_SYMBOLS。
    """
    symbols: list[str] = []
    if override:
        for raw in override:
            text = str(raw or "").strip().upper()
            if text:
                symbols.append(text)
    else:
        try:
            from app.services import watchlist

            for entry in watchlist.list_symbols():
                symbols.append(str(entry.get("symbol") or "").strip().upper())
        except Exception as e:  # noqa: BLE001
            logger.warning("读取自选失败 (研报落地将只覆盖持仓): %s", e)
        try:
            from app.services import manual_holdings

            for symbol in manual_holdings.load_all(data_dir):
                symbols.append(str(symbol or "").strip().upper())
        except Exception as e:  # noqa: BLE001
            logger.warning("读取持仓失败: %s", e)

    seen: set[str] = set()
    ordered: list[str] = []
    for symbol in symbols:
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        ordered.append(symbol)
    ordered.sort()
    if len(ordered) > MAX_SYMBOLS:
        logger.warning("待查标的 %d 个超过上限 %d, 只取前 %d 个",
                       len(ordered), MAX_SYMBOLS, MAX_SYMBOLS)
        ordered = ordered[:MAX_SYMBOLS]
    names = name_lookup(data_dir)
    return [{"symbol": symbol, "name": names.get(symbol, "")} for symbol in ordered]


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------

def search_docs(
    client, keyword: str, *, top_k: int = DEFAULT_TOP_K,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict:
    """单关键词检索 → 去重 + 本地阈值过滤后的文档列表 (API 与落地共用)。"""
    records, msg = client.research_search(keyword, top_k=top_k, threshold=threshold)
    docs = filter_by_threshold(dedupe_docs(records), threshold)
    return {
        "keyword": keyword,
        "top_k": top_k,
        "threshold": threshold,
        "raw_count": len(records),
        "doc_count": len(docs),
        "message": msg or "",
        "items": [doc_view(doc) for doc in docs],
    }


def collect_notes(
    client, symbols: list[dict], *, top_k: int = DEFAULT_TOP_K,
    threshold: float = DEFAULT_THRESHOLD, source_date: str = "",
) -> list[dict]:
    """逐标的检索 (串行, 见 MAX_CONCURRENCY 说明) → 扩展表行。

    单个标的失败只 warning, 不拖垮整轮 —— 与热股 `collect_boards` 同纪律。
    没有命中的标的不落行 (不留空壳行, 与 `ext_hot_*` 一致)。
    """
    rows: list[dict] = []
    for index, entry in enumerate(symbols, start=1):
        symbol = str(entry.get("symbol") or "")
        name = str(entry.get("name") or "")
        keyword = query_for(symbol, name)
        try:
            result = search_docs(client, keyword, top_k=top_k, threshold=threshold)
        except Exception as e:  # noqa: BLE001
            logger.warning("研报检索失败 %s (%s): %s", symbol, keyword, e)
            continue
        docs = result["items"]
        logger.info("[%d/%d] %s %s -> 原始 %d 片段 / %d 篇",
                    index, len(symbols), symbol, keyword, result["raw_count"], len(docs))
        if not docs:
            continue
        rows.append(to_ext_row(symbol, name, docs, source_date))
    return rows


async def fetch_rows(
    config: ExtConfig, target_date: date, data_dir: Path | None = None,
) -> list[dict]:
    """adapter 入口: 由 `ext_pull._fetch_adapter_rows` 调用, 只产出行。"""
    from app.plugins.pingan.client import PingAnClient
    from app.plugins.pingan.provider import get_api_key

    adapter = (config.pull.adapter if config.pull else None) or {}
    top_k = int(adapter.get("top_k") or DEFAULT_TOP_K)
    threshold = float(adapter.get("threshold") or DEFAULT_THRESHOLD)
    override = adapter.get("symbols") or None

    api_key = get_api_key()
    if not api_key:
        raise ValueError(
            "未配置平安 API Key — 请到 设置 → 数据源 → 平安证券 填写 (secrets: pingan_api_key)"
        )

    base = Path(data_dir) if data_dir else Path("data")
    symbols = tracked_symbols(base, override)
    if not symbols:
        raise ValueError("没有可跟踪的标的 (自选与持仓都为空)")

    source_date = target_date.isoformat()
    client = PingAnClient(api_key, timeout=REQUEST_TIMEOUT_S)
    try:
        rows = collect_notes(client, symbols, top_k=top_k, threshold=threshold,
                             source_date=source_date)
    finally:
        client.close()
    if not rows:
        raise ValueError(
            f"研报检索 0 行 ({len(symbols)} 个标的全部无命中; 阈值 {threshold} 可能过高)"
        )
    logger.info("研报落地: %d/%d 个标的命中, 日期 %s", len(rows), len(symbols), source_date)
    return rows


def live_docs(
    keyword: str, *, top_k: int = DEFAULT_TOP_K, threshold: float = DEFAULT_THRESHOLD,
) -> dict:
    """即时检索 (API 用)。"""
    from app.plugins.pingan.client import PingAnClient
    from app.plugins.pingan.provider import get_api_key

    api_key = get_api_key()
    if not api_key:
        raise ValueError(
            "未配置平安 API Key — 请到 设置 → 数据源 → 平安证券 填写 (secrets: pingan_api_key)"
        )
    client = PingAnClient(api_key, timeout=REQUEST_TIMEOUT_S)
    try:
        return search_docs(client, keyword, top_k=top_k, threshold=threshold)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 内置预设
# ---------------------------------------------------------------------------

def preset() -> ExtConfig:
    """内置扩展表 `ext_research_note`: 自选+持仓的当日最佳研报, 每交易日收盘后落库。"""
    from app.plugins.pingan.client import BASE_URL, RESEARCH_PATH

    return ExtConfig(
        id=CONFIG_ID,
        label="研报正文",
        mode="timeseries",
        fields=[ExtField(name, dtype, label) for name, dtype, label in FIELDS],
        description=(
            "自选+持仓标的的研报正文片段 (平安研报检索)。"
            "每个标的每日一行 = 当日命中最优的那篇; 多篇浏览走 /api/research-notes/search。"
            "源端片段不含机构/评级/发布日期, 所以本表不落这三列 —— 看片段与 titles, "
            "或点开 doc_id 对应的原文链接。"
        ),
        symbol_map={},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=f"{BASE_URL}/{RESEARCH_PATH}",
            method="POST",
            response_path="records",
            # 凭据复用行情插件同一把 Key (secrets.json: pingan_api_key);
            # 注意这个接口还要额外一个平台内置 Bearer token, 由 client 固定补上。
            auth={"type": "header", "header": "X-API-Key", "key_ref": "pingan_api_key"},
            timeout_seconds=int(REQUEST_TIMEOUT_S),
            # 收盘后跑: 排在热股 (17:10) 与 ETF 估值 (17:20) 之后, 顺带避开盘中轮询
            schedule_time=SCHEDULE_TIME,
            schedule_period="daily",
            schedule_trading_only=True,
            enabled=True,
            adapter={
                "kind": ADAPTER_KIND,
                "top_k": DEFAULT_TOP_K,
                "threshold": DEFAULT_THRESHOLD,
                "symbols": [],
            },
        ),
    )


def get_preset(config_id: str) -> ExtConfig | None:
    return preset() if config_id == CONFIG_ID else None


def _is_factory_shell(pull: PullConfig | None) -> bool:
    """现有 pull 是否为空壳 (无 URL / 无 adapter) —— 需要补出厂配方。"""
    if pull is None:
        return True
    if not (pull.url or "").strip():
        return True
    return not pull.adapter


async def ensure_preset(data_dir: Path) -> None:
    """启动时播种内置研报表 (独立标记, 只播一次)。

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
            logger.warning("研报播种标记写入失败 (下次启动会重试): %s", e)
