"""手工录入的真实持仓 —— 不玩模拟盘、也不接券商接口的人也能用组合分析。

用户要求（2026-11-14）：
「持仓总览跟行动卡这两个是否还没开发完成的」→ 盘查发现 ``实际持仓`` 的唯一
来源是 ``data/paper/accounts/<acc>/`` 的模拟盘账本。真实持仓进不来，后果很硬：

- 不玩模拟盘 → 每只票永远显示「自选观察」，行动卡的持仓段不出现；
- 「跌 10% 对账户影响多少」「行业/主题重复暴露」全部算不出来，因为
  没有真实权重。

本模块补的就是这一环：让用户自己录 **数量 / 成本 / 建仓日**，够算权重、
成本、浮盈亏、持仓期限。它与模拟盘是**并列的两个口径**，不是替代关系。

三条边界（写死在实现里）：

1. **不假装与券商对账过。** 口径标签写「手工录入」，limitations 明说
   「未与券商对账」。券商实际成交可能有手续费、分红送股、部分成交，
   这里的成本是多少就按多少算，不做还原。
2. **现金要么录要么说明。** 权重分母是账户权益 = 录入的现金 + 持仓市值。
   现金没录就按 0 处理并明确提示「权重合计会等于 1，看起来像满仓」——
   不悄悄把它当成满仓事实。
3. **不做 T+1 / 可用量的推断。** 手工录入没有流水，算不出可用数量，
   就如实不给（``available_qty`` 为 None），而不是拿总数量冒充。

存储：``data/user_data/manual_holdings/holding_<symbol>.json``（沿用
``JsonDefinitionStore`` 的约定），账户级现金单独放同一目录的 ``account.json``。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.definitions import JsonDefinitionStore

logger = logging.getLogger(__name__)

#: 手工录入持仓的存储目录（相对 data_dir）。
STORE_DIR = "user_data/manual_holdings"
#: 账户级设置（目前只有现金）。
ACCOUNT_FILE = "account.json"

#: 数量上限：再大也不会是散户的真实持仓，挡掉手滑多打几个 0。
MAX_QTY = 1_000_000_000
#: 成本上限（元）：A 股最高价约 2000 元，留足余量。
MAX_AVG_COST = 100_000.0
#: 现金上限（元）。
MAX_CASH = 1_000_000_000_000.0

#: 支持手工录入的资产类型。**不含 index** —— 指数不能直接持有，
#: 且 ``paper._daily_path`` 对 index 会回落到 ``kline_daily`` 读错目录。
_SUPPORTED_ASSET_TYPES = ("stock", "etf")


def _now_iso() -> str:
    import datetime as dt

    return dt.date.today().isoformat()


# ── 归一化与校验 ────────────────────────────────────────

def normalize_holding(raw: dict) -> dict:
    """补默认值（不校验），供 store 读取时调用。坏值一律留空而不是猜。"""
    d = dict(raw)
    d["symbol"] = str(d.get("symbol") or "").strip().upper()
    d["asset_type"] = str(d.get("asset_type") or "stock").strip().lower()
    d.setdefault("name", "")
    d.setdefault("note", "")
    d.setdefault("entry_date", None)
    d.setdefault("created_at", _now_iso())
    d.setdefault("updated_at", d["created_at"])
    # 成本口径：旧文件没有这个字段，默认「券商展示价(不复权)」——
    # 这正是历史录入一直隐含的口径（与现价 raw_close 同尺度），补默认值
    # 才能让老数据不经过迁移也读得出来，且不会被误判成「口径未知」。
    d.setdefault("cost_basis", "broker_display")
    d["cost_basis"] = str(d["cost_basis"] or "broker_display").strip().lower()
    # 数量/成本保持原始类型，校验阶段再判；读取时不再转换，
    # 免得坏数据被静默改成 0 之后看起来像"没有持仓"。
    return d


def validate_holding(h: dict) -> None:
    """校验并抛 ValueError（中文信息）。fail-closed —— 不静默修正用户的数字。"""
    symbol = str(h.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("标的代码不能为空")

    asset_type = str(h.get("asset_type") or "stock").strip().lower()
    if asset_type not in _SUPPORTED_ASSET_TYPES:
        raise ValueError(f"asset_type 只支持 {'/'.join(_SUPPORTED_ASSET_TYPES)}")

    qty = h.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, (int, float)):
        raise ValueError("持仓数量必须是数字")
    # NaN 必须在这里挡掉：NaN <= 0 和 NaN > MAX_QTY 都是 False，会一路落到下面的
    # 整数股校验，int(NaN) 抛英文的 "cannot convert float NaN to integer"，
    # 把内部异常暴露成用户看到的 400 原因。
    if qty != qty or qty in (float("inf"), float("-inf")):
        raise ValueError("持仓数量必须是有效数字")
    if qty <= 0:
        raise ValueError("持仓数量必须大于 0；不持有请删除该条记录")
    if qty > MAX_QTY:
        raise ValueError(f"持仓数量超过上限 {MAX_QTY}，请检查是否多输了几个 0")
    if asset_type == "stock" and float(qty) != int(qty):
        raise ValueError("股票数量必须是整数股（ETF 同理按份额整数填）")

    cost = h.get("avg_cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        raise ValueError("成本价必须是数字")
    if cost != cost or cost in (float("inf"), float("-inf")):
        raise ValueError("成本价必须是有效数字")
    # 成本允许为负：反复做 T 把成本摊薄后，券商 App 里成本确实会显示成负数，
    # 照实录即可（权重按市值算、盈亏金额照常；只有收益率%因分母为负不适用）。
    # 0 在录入约定里代表"没填"，仍拒绝；绝对值上限同时挡住多打几个 0 的正负数。
    if cost == 0:
        raise ValueError("成本价不能为 0；做 T 摊薄成负数可以照填")
    if abs(cost) > MAX_AVG_COST:
        raise ValueError(f"成本价绝对值超过上限 {MAX_AVG_COST}，请检查单位（这里填每股价格）")

    # 成本口径写进契约：只允许两个明确值，未知值拒绝而不是猜成默认。
    # 前复权口径需要复权因子才能与现价对齐，口径不对齐时 pnl 会静默全错。
    from app.services.holding_corporate_actions import COST_BASIS_CHOICES

    basis = str(h.get("cost_basis") or "broker_display").strip().lower()
    if basis not in COST_BASIS_CHOICES:
        raise ValueError(f"cost_basis 只支持 {'/'.join(COST_BASIS_CHOICES)}")

    entry = h.get("entry_date")
    if entry:
        import datetime as dt

        try:
            dt.date.fromisoformat(str(entry)[:10])
        except ValueError as exc:
            raise ValueError("建仓日期格式应为 YYYY-MM-DD") from exc


# ── 存储 ────────────────────────────────────────────────

_HOLDING_STORE = JsonDefinitionStore(
    STORE_DIR,
    glob_pattern="holding_*.json",
    label="manual_holding",
    normalize=normalize_holding,
)


def _file_id(symbol: str) -> str:
    """存储文件名用的 id。store 按 id 落盘，必须与 glob ``holding_*.json`` 匹配。"""
    return f"holding_{symbol}"


def load_all(data_dir: Path) -> dict[str, dict]:
    """全部手工持仓，以 symbol 为键。"""
    out: dict[str, dict] = {}
    for row in _HOLDING_STORE.load_all(data_dir):
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            raw_id = str(row.get("id") or "")
            if raw_id.startswith("holding_"):
                symbol = raw_id[len("holding_"):].upper()
        if symbol:
            out[symbol] = {**row, "symbol": symbol}
    return out


def load_one(data_dir: Path, symbol: str) -> dict | None:
    return load_all(data_dir).get(str(symbol).strip().upper())


def save_one(data_dir: Path, holding: dict) -> dict:
    symbol = str(holding.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("标的代码不能为空")
    normalized = normalize_holding({**holding, "symbol": symbol})
    validate_holding(normalized)
    existing = load_one(data_dir, symbol)
    normalized["updated_at"] = _now_iso()
    if existing and existing.get("created_at"):
        normalized["created_at"] = existing["created_at"]
    _HOLDING_STORE.save_one(data_dir, {**normalized, "id": _file_id(symbol)})
    return normalized


def delete_one(data_dir: Path, symbol: str) -> bool:
    return _HOLDING_STORE.delete_one(data_dir, _file_id(str(symbol).strip().upper()))


# ── 账户级设置（现金） ──────────────────────────────────

def _account_path(data_dir: Path) -> Path:
    return Path(data_dir) / STORE_DIR / ACCOUNT_FILE


def load_account(data_dir: Path) -> dict:
    """账户级设置。损坏/缺失 → ``{"cash": None}``，不抛。"""
    path = _account_path(data_dir)
    if not path.exists():
        return {"cash": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("手工持仓账户设置读取失败，按未设置处理: %s", path)
        return {"cash": None}
    if not isinstance(raw, dict):
        return {"cash": None}
    cash = raw.get("cash")
    if isinstance(cash, bool) or not isinstance(cash, (int, float)):
        cash = None
    elif cash != cash or cash in (float("inf"), float("-inf")):
        # 旧版本曾把 NaN 写进文件（json 允许 NaN 字面量但浏览器不允许），
        # 读回时按「未录入」处理，避免把 NaN 再喂给权重分母。
        logger.warning("手工持仓账户现金是非法数字(NaN/Inf)，按未录入处理: %s", path)
        cash = None
    return {"cash": float(cash) if cash is not None else None}


def save_account(data_dir: Path, *, cash: float | None) -> dict:
    """写账户级现金。``None`` 表示"不录入"，会从文件里删掉这个字段。"""
    if cash is not None:
        if isinstance(cash, bool) or not isinstance(cash, (int, float)):
            raise ValueError("现金必须是数字")
        # NaN 必须显式拒绝：NaN < 0 与 NaN > MAX_CASH 都是 False，会穿过全部比较，
        # 最终 json.dumps 写出一行 {"cash": NaN} —— 这不是合法 JSON，
        # 浏览器 JSON.parse 直接抛错，整个持仓面板读不出来。
        if cash != cash or cash in (float("inf"), float("-inf")):
            raise ValueError("现金必须是有效数字")
        if cash < 0:
            raise ValueError("现金不能为负")
        if cash > MAX_CASH:
            raise ValueError(f"现金超过上限 {MAX_CASH}")
    payload: dict[str, Any] = {"cash": None if cash is None else round(float(cash), 2)}
    path = _account_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    except OSError as exc:                                   # pragma: no cover - 磁盘异常
        logger.warning("手工持仓账户设置写入失败: %s", exc)
        raise ValueError("账户设置写入失败，请检查数据目录权限") from exc
    return payload
