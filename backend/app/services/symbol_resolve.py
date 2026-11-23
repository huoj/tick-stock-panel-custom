"""把用户输入的标的（代码 / 裸 6 位 / 名称）解析成标准 symbol。

## 为什么要单独一个模块

行动卡、持仓总览、买入计划这些入口都直接吃用户输入框里的字符串。用户按名称
输入（"美的"）在交易场景下非常自然，但下游读 K 线只认 ``000333.SZ`` 这种
``<code>.<exchange>`` 形式。这些入口原来把原文当 symbol 直接往下传，于是报出

    美的 没有可用日线数据 (asset_type=stock)

—— 这句话对用户毫无信息量：他输入的确实是"美的"，数据也确实存在，只是没人
做过名称到代码的解析。这里统一收口，并把失败原因分清楚：

- ``empty``: 没输入
- ``not_found``: 本地维表里找不到（可能不是 A 股 / 维表还没同步）
- ``ambiguous``: 有多个同名或相近标的，必须让用户选
- ``code`` / ``name_exact`` / ``name_prefix`` / ``name_contains``: 解析成功

## 不做的事

不去猜用户的意图（比如"美的"命中"美的电器"和"美的集团"时不擅自挑一个），
也不联网补全 —— 维表没同步就是没同步，如实报未找到，让人去同步数据而不是
让人以为这只票不存在。
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

_SYMBOL_RE = re.compile(r"^(\d{6})\.(SZ|SH|BJ)$")
_PREFIXED_RE = re.compile(r"^(SZ|SH|BJ)(\d{6})$")
_BARE_RE = re.compile(r"^\d{6}$")
# 裸代码补交易所后缀: 6/9 开头沪市, 0/3 深市, 4/8 北交所, 5 沪市基金
_PREFIX_HINT = (
    ("6", "SH"),
    ("9", "SH"),
    ("5", "SH"),
    ("0", "SZ"),
    ("3", "SZ"),
    ("1", "SZ"),
    ("4", "BJ"),
    ("8", "BJ"),
)

_ASSET_TYPES = ("stock", "etf")
_MAX_CANDIDATES = 8


@dataclass
class Resolution:
    """一次解析的结果。``symbol`` 为 None 表示没解析出来。"""

    query: str
    symbol: str | None = None
    name: str | None = None
    asset_type: str | None = None
    reason: str = "empty"
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.symbol is not None

    def error_message(self) -> str:
        """给用户看的 404 文案：说清是哪一步没成、下一步该输入什么。"""
        if self.reason == "empty":
            return "请输入标的代码或名称（如 000333.SZ / 美的集团）。"
        if self.reason == "ambiguous":
            listed = "、".join(f"{c['symbol']} {c['name']}" for c in self.candidates)
            return f"「{self.query}」匹配到多只标的：{listed} —— 请改用代码或更完整的名称。"
        return (
            f"未找到「{self.query}」对应的标的。"
            "可以输入 6 位代码（000333）、带交易所后缀的代码（000333.SZ）或股票名称；"
            "ETF 也能查。若确认代码正确但仍找不到，多半是本地维表还没同步这只标的。"
        )


def _clean(text: str) -> str:
    """去空白、全角转半角、去掉括号里的补充说明（"美的集团(000333)"）。"""
    s = (text or "").strip()
    if not s:
        return ""
    out = []
    for ch in s:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:  # 全角空格
            out.append(" ")
        else:
            out.append(ch)
    s = "".join(out).strip()
    s = re.sub(r"[(（][^)）]*[)）]", "", s).strip()
    return s


def _instrument_rows(repo: Any) -> Iterable[tuple[str, str, str]]:
    """(symbol, name, asset_type) 三元组；维表缺列或读失败就跳过该类资产。"""
    for asset_type in _ASSET_TYPES:
        try:
            df = repo.get_instruments_asset(asset_type)
        except Exception:  # noqa: BLE001 — 维表读失败降级为"该类资产不存在"
            continue
        if df is None or df.is_empty() or "symbol" not in df.columns:
            continue
        has_name = "name" in df.columns
        symbols = df["symbol"].cast(str).to_list()
        names = df["name"].cast(str).to_list() if has_name else [""] * len(symbols)
        for symbol, name in zip(symbols, names, strict=False):
            yield str(symbol), (name or ""), asset_type


def _code_form(query: str) -> str | None:
    """把纯代码形态归一成 ``000333.SZ``; 不是代码形态就返回 None。

    只做形态推断, 不查维表 —— 供"维表不可用时按原样放行"使用。
    裸 6 位按交易所前缀猜, 猜不中时保留裸代码 (上层仍能查, 查不到就是空)。
    """
    upper = query.upper()
    if _SYMBOL_RE.match(upper):
        return upper
    m = _PREFIXED_RE.match(upper)
    if m:
        return f"{m.group(2)}.{m.group(1)}"
    if _BARE_RE.match(upper):
        for prefix, exchange in _PREFIX_HINT:
            if upper.startswith(prefix):
                return f"{upper}.{exchange}"
        return upper
    return None


def _row(symbol: str, name: str, asset_type: str) -> dict[str, Any]:
    return {"symbol": symbol, "name": name, "asset_type": asset_type}


def resolve(repo: Any, text: str) -> Resolution:
    """解析用户输入。优先精确代码，其次裸代码，最后名称（精确 → 前缀 → 包含）。"""
    query = _clean(text)
    res = Resolution(query=query)
    if not query:
        res.reason = "empty"
        return res

    rows = list(_instrument_rows(repo))
    if not rows:
        # 维表不可用(空库/测试桩/首次启动尚未同步): 不能因此把用户挡在门外。
        # 代码形态的输入按原样放行 —— 解析是"锦上添花", 不该成为硬依赖;
        # 名称形态无论如何都解析不了, 如实说找不到。
        guess = _code_form(query)
        if guess:
            return Resolution(
                query=query,
                symbol=guess,
                reason="code_unverified",
            )
        res.reason = "not_found"
        return res
    by_symbol = {symbol.upper(): (symbol, name, asset_type) for symbol, name, asset_type in rows}

    upper = query.upper()

    # 1) 完整代码 000333.SZ
    m = _SYMBOL_RE.match(upper)
    if m:
        hit = by_symbol.get(upper)
        if hit:
            return _hit(res, hit, "code")
        res.reason = "not_found"
        return res

    # 2) 前缀式代码 SZ000333
    m = _PREFIXED_RE.match(upper)
    if m:
        cand = f"{m.group(2)}.{m.group(1)}"
        hit = by_symbol.get(cand)
        if hit:
            return _hit(res, hit, "code")
        res.reason = "not_found"
        return res

    # 3) 裸 6 位代码：先按前缀猜交易所，猜不中就找所有同代码的
    if _BARE_RE.match(upper):
        for prefix, exchange in _PREFIX_HINT:
            if upper.startswith(prefix):
                hit = by_symbol.get(f"{upper}.{exchange}")
                if hit:
                    return _hit(res, hit, "code")
                break
        same_code = [v for k, v in by_symbol.items() if k.split(".")[0] == upper]
        if len(same_code) == 1:
            return _hit(res, same_code[0], "code")
        if same_code:
            res.reason = "ambiguous"
            res.candidates = [_row(*v) for v in same_code[:_MAX_CANDIDATES]]
            return res
        res.reason = "not_found"
        return res

    # 4) 名称：精确 → 前缀 → 包含（逐级放宽，命中即停，避免"美的"直接掉到模糊）
    lowered = query.lower()
    exact = [v for v in by_symbol.values() if v[1].lower() == lowered]
    if len(exact) == 1:
        return _hit(res, exact[0], "name_exact")
    if len(exact) > 1:
        res.reason = "ambiguous"
        res.candidates = [_row(*v) for v in exact[:_MAX_CANDIDATES]]
        return res

    starts = [v for v in by_symbol.values() if v[1].lower().startswith(lowered)]
    if len(starts) == 1:
        return _hit(res, starts[0], "name_prefix")

    contains = [v for v in by_symbol.values() if lowered in v[1].lower()]
    if len(contains) == 1:
        return _hit(res, contains[0], "name_contains")
    if len(contains) > 1:
        # 前缀命中优于包含命中：如果恰好只有一只以它开头，就是它
        if len(starts) == 1:
            return _hit(res, starts[0], "name_prefix")
        pool = starts if starts else contains
        res.reason = "ambiguous"
        res.candidates = [_row(*v) for v in pool[:_MAX_CANDIDATES]]
        return res

    res.reason = "not_found"
    return res


def _hit(res: Resolution, row: tuple[str, str, str], reason: str) -> Resolution:
    res.symbol, res.name, res.asset_type = row
    res.reason = reason
    return res


def resolve_or_404(repo: Any, text: str, *, what: str = "标的") -> Resolution:
    """解析失败就抛 404，成功返回结果（供 API 层直接调用）。"""
    from fastapi import HTTPException

    res = resolve(repo, text)
    if not res.ok:
        raise HTTPException(status_code=404, detail=res.error_message())
    _ = what
    return res


def resolve_many(repo: Any, texts: Iterable[str]) -> tuple[list[Resolution], list[str]]:
    """批量解析（逗号分隔的输入）。返回 (成功的, 失败原因列表)。"""
    ok: list[Resolution] = []
    errors: list[str] = []
    for text in texts:
        res = resolve(repo, text)
        if res.ok:
            ok.append(res)
        else:
            errors.append(res.error_message())
    return ok, errors
