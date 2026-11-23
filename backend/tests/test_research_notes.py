"""研报检索落地 (平安 skill=research-report) 测试。

覆盖四层:
  1. 行归一 —— 片段压缩 / 标题概览 / 替身 doc_id 不冒充真 id。
     **重点: 不落机构·评级·发布日期** —— 源端片段抽不出可信值, 编一个比缺更糟
  2. 文档去重 —— 同一篇研报被切成多片段, 不去重会把表挤满; 阈值只能本地做
  3. 取数 —— 检索词形态 (6 位代码优先) / 单标的失败不拖垮整轮 / 跟踪集合来源
  4. 预设与接线 —— adapter 配方 / 独立播种标记 / 路由注册
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

from app.services.ext_data import ExtConfig, ExtConfigStore, PullConfig
from app.services.research_notes import (
    ADAPTER_KIND,
    CONFIG_ID,
    DEFAULT_THRESHOLD,
    DEFAULT_TOP_K,
    FIELD_NAMES,
    FIELDS,
    MAX_SYMBOLS,
    MAX_TOP_K,
    SNIPPET_CHARS,
    TITLE_MAX_CHARS,
    TITLES_KEEP,
    collect_notes,
    dedupe_docs,
    doc_key,
    doc_view,
    ensure_preset,
    filter_by_threshold,
    get_preset,
    is_real_doc_id,
    make_snippet,
    make_titles,
    preset,
    query_for,
    search_docs,
    to_ext_row,
    tracked_symbols,
)

DAY = date(2026, 11, 19)

# 真实片段形态 (2026-11-19 实测, 保留源端给的样子):
# 只有 content / metadata{path,description} / score / title —— 无机构无日期无类型。
# RAW_SKELETON 是实测到的「骨架片段」: 全文只有标题 + 核心观点标题 + 股票代码。
RAW_A = {
    "content": (
        "证券研究报告 | 半年报点评 2026年07月26日 国盛证券\n"
        "宁德时代(300750.SZ)：业绩超预期，盈利能力持续提升\n"
        "投资评级 买入 | 维持\n"
        "预计公司2026-2028年归母净利润分别为...\n"
    ),
    "metadata": {
        "path": "/profile_platform/knowledge-base/5/knowledge/document-detail?id=9168&kb_id=520",
        "description": "",
    },
    "score": 0.6675,
    "title": "宁德时代2026年中报点评：出货高增业绩稳健，回购彰显龙头信心.pdf",
}
RAW_B = {
    "content": "成本与零售挤压，盈利能力承压.pdf 相关研究：《美的集团(000333.SZ)2026年一季报点评》2026-04-28",
    "metadata": {
        "path": "/profile_platform/knowledge-base/5/knowledge/document-detail?id=10898&kb_id=520",
        "description": "",
    },
    "score": 0.4211,
    "title": "成本与零售挤压，盈利能力承压.pdf",
}
RAW_C = {
    "content": "中国银河证券股份有限公司 家用电器产业亮点之十四：厄尔尼诺气候下，如何展望空调需求",
    "metadata": {
        "path": "/profile_platform/knowledge-base/5/knowledge/document-detail?id=10898&kb_id=520",
        "description": "",
    },
    "score": 0.5120,
    "title": "家用电器产业亮点之十四.pdf",
}
RAW_NO_ID = {
    "content": "长江证券 关于某公司的点评 投资评级 增持",
    "metadata": {"path": "", "description": ""},
    "score": 0.35,
    "title": "无 id 的片段.pdf",
}
RAW_NO_KEY = {
    "content": "既没有 id 也没有标题",
    "metadata": {"path": "", "description": ""},
    "score": 0.31,
    "title": "",
}
# 实测原文 (score=0.6343): 只有标题层级与代码, 没有任何元数据
RAW_SKELETON = {
    "content": "# 成本与零售挤压，盈利能力承压\n## 核心观点\n### 格力电器 (股票代码: 000651)\n-",
    "metadata": {
        "path": "/profile_platform/knowledge-base/5/knowledge/document-detail?id=20697&kb_id=520",
        "description": "",
    },
    "score": 0.6343,
    "title": "成本与零售挤压，盈利能力承压.pdf",
}
# 实测原文: 正文里的数据时点 (「截至2026年7月31日尚未回购」), 不是发布日期
RAW_DATA_POINT = {
    "content": "# 4. 本周家电公司公告\n## 格力电器：关于股份回购进展情况的公告 "
               "截至2026年7月31日，公司尚未实施本次股份回购。",
    "metadata": {
        "path": "/profile_platform/knowledge-base/5/knowledge/document-detail?id=20500&kb_id=520",
        "description": "",
    },
    "score": 0.187,
    "title": "家电行业周报.pdf",
}


# ---------------------------------------------------------------------------
# 1. 行归一
# ---------------------------------------------------------------------------

def test_doc_key_prefers_metadata_id() -> None:
    assert doc_key(RAW_A) == "9168"
    assert doc_key(RAW_B) == "10898"


def test_doc_key_falls_back_to_title() -> None:
    assert doc_key(RAW_NO_ID) == "title:无 id 的片段.pdf"
    assert doc_key(RAW_NO_KEY) == "", "既无 id 又无标题必须返回空 (无法归因)"


def test_is_real_doc_id_rejects_title_stand_in() -> None:
    """`title:` 前缀只是去重用的替身, 落到表里会把标题塞进 doc_id 列。"""
    assert is_real_doc_id("20697") is True
    assert is_real_doc_id("title:某研报.pdf") is False
    assert is_real_doc_id("") is False


def test_to_ext_row_leaves_doc_id_empty_for_title_only_key() -> None:
    docs = dedupe_docs([RAW_NO_ID])
    row = to_ext_row("600941.SH", "中国移动", docs, "2026-11-19")
    assert row["doc_id"] == "", "没有真 id 就留空, 不要写替身键"
    assert row["title"] == "无 id 的片段.pdf"


def test_make_snippet_compacts_and_truncates() -> None:
    assert make_snippet("a\n\n  b   c") == "a b c"
    long_text = "字" * (SNIPPET_CHARS + 50)
    out = make_snippet(long_text)
    assert len(out) <= SNIPPET_CHARS + 1
    assert out.endswith("…")


def test_make_titles_keeps_top_n_and_flattens_pipes() -> None:
    docs = dedupe_docs([RAW_A, RAW_B, RAW_C])
    titles = make_titles(docs)
    parts = titles.split("|")
    assert len(parts) <= TITLES_KEEP
    assert "宁德时代2026年中报点评" in parts[0], "按相关度降序取最优的那篇在前"
    assert all("|" not in p.replace("｜", "") for p in parts)


def test_make_titles_truncates_long_titles() -> None:
    long_title = "非常长的标题" * 20 + ".pdf"
    out = make_titles([{"title": long_title}], keep=1)
    assert len(out) <= TITLE_MAX_CHARS + 1
    assert out.endswith("…")


def test_make_titles_skips_blank_titles() -> None:
    assert make_titles([{"title": ""}, {"title": "有标题.pdf"}]) == "有标题.pdf"
    assert make_titles([]) == ""


def test_row_has_no_heuristic_metadata_columns() -> None:
    """实测机构 1/27(且错)、评级 0/27、可信日期 1/27 —— 这三类一律不落列。

    这条用例是防止「好心加回来」: 只要有人加列, 就会有一批假数据进表。
    """
    row = to_ext_row("000651.SZ", "格力电器", dedupe_docs([RAW_SKELETON]), "2026-11-19")
    for banned in ("institution", "rating", "note_date", "date_source"):
        assert banned not in row, f"{banned} 已实测不可靠, 不该出现在落库列里"
    assert set(row) == set(FIELD_NAMES)


def test_skeleton_fragment_yields_only_what_it_has() -> None:
    """实测骨架片段 (只有标题层级 + 代码) 不该凭空长出别的字段。"""
    row = to_ext_row("000651.SZ", "格力电器", dedupe_docs([RAW_SKELETON]), "2026-11-19")
    assert row["doc_id"] == "20697"
    assert row["top_score"] == pytest.approx(0.6343)
    assert row["snippet"].startswith("# 成本与零售挤压")
    assert row["source_date"] == "2026-11-19"


def test_data_point_date_in_body_is_not_promoted() -> None:
    """正文里的「截至2026年7月31日尚未回购」是数据时点, 不可能是发布日期。"""
    row = to_ext_row("000651.SZ", "格力电器", dedupe_docs([RAW_DATA_POINT]), "2026-11-19")
    assert "2026-07-31" not in str(row.values()), "正文日期不许被当元数据写进行"


# ---------------------------------------------------------------------------
# 2. 文档去重与阈值
# ---------------------------------------------------------------------------

def test_dedupe_docs_keeps_best_fragment_per_document() -> None:
    docs = dedupe_docs([RAW_A, RAW_B, RAW_C])
    assert len(docs) == 2, "10898 有两个片段, 只该留一条"
    by_id = {d["doc_id"]: d for d in docs}
    assert by_id["10898"]["score"] == pytest.approx(0.5120), "留高分那段"
    assert by_id["9168"]["score"] == pytest.approx(0.6675)


def test_dedupe_docs_sorted_desc_and_drops_unattributable() -> None:
    docs = dedupe_docs([RAW_B, RAW_A, RAW_NO_KEY])
    assert [d["doc_id"] for d in docs] == ["9168", "10898"]


def test_dedupe_docs_handles_empty_and_bad_score() -> None:
    assert dedupe_docs([]) == []
    docs = dedupe_docs([{**RAW_A, "score": "不是数字"}])
    assert docs[0]["score"] == 0.0


def test_filter_by_threshold_is_local() -> None:
    """服务端不认 score_threshhold, 阈值只能在这里生效。"""
    docs = dedupe_docs([RAW_A, RAW_B])
    assert len(filter_by_threshold(docs, 0.6)) == 1
    assert len(filter_by_threshold(docs, 0.05)) == 2
    assert filter_by_threshold(docs, 0.99) == []


# ---------------------------------------------------------------------------
# 3. 检索词形态与行归一
# ---------------------------------------------------------------------------

def test_query_for_prefers_six_digit_code() -> None:
    """实测代码召回 15~19 篇全命中, 名称只召回 6 篇 —— 有代码就用代码。"""
    assert query_for("600941.SH", "中国移动") == "600941"
    assert query_for("159529.SZ", "标普消费ETF") == "159529"
    assert query_for("00700.HK", "腾讯控股") == "腾讯控股", "非 6 位代码退化为名称"
    assert query_for("00700.HK", "") == "00700"


def test_to_ext_row_full() -> None:
    docs = dedupe_docs([RAW_A, RAW_B, RAW_C])
    row = to_ext_row("300750.SZ", "宁德时代", docs, "2026-11-19")
    assert row["symbol"] == "300750.SZ"
    assert row["code"] == "300750"
    assert row["name"] == "宁德时代"
    assert row["doc_count"] == 2
    assert row["top_score"] == pytest.approx(0.6675)
    assert row["doc_id"] == "9168"
    assert row["title"].startswith("宁德时代2026年中报点评")
    assert row["titles"].startswith("宁德时代2026年中报点评")
    assert row["source_date"] == "2026-11-19"
    assert set(row) == set(FIELD_NAMES), "落库列必须与 FIELDS 完全对齐"


def test_to_ext_row_without_docs() -> None:
    row = to_ext_row("000651.SZ", "格力电器", [], "2026-11-19")
    assert row["doc_count"] == 0
    assert row["top_score"] is None
    assert row["title"] == ""
    assert row["titles"] == ""
    assert row["doc_id"] == ""


def test_doc_view_shape() -> None:
    view = doc_view(dedupe_docs([RAW_A])[0])
    assert view["doc_id"] == "9168"
    assert view["path"].endswith("id=9168&kb_id=520")
    assert view["snippet"]
    assert view["score"] == pytest.approx(0.6675)
    for banned in ("note_date", "institution", "rating", "date_source"):
        assert banned not in view, "API 也不许返回猜出来的元数据"


def test_doc_view_keeps_title_for_title_only_key() -> None:
    view = doc_view(dedupe_docs([RAW_NO_ID])[0])
    assert view["doc_id"] == ""
    assert view["title"] == "无 id 的片段.pdf"


def test_fields_are_ordered_and_typed() -> None:
    names = [name for name, _, _ in FIELDS]
    assert names == list(FIELD_NAMES)
    types = {name: dtype for name, dtype, _ in FIELDS}
    assert types["doc_count"] == "int"
    assert types["top_score"] == "float"
    assert types["symbol"] == "str"


# ---------------------------------------------------------------------------
# 4. 取数
# ---------------------------------------------------------------------------

class _FakeClient:
    """按 keyword 返回预设片段, 记录调用参数。"""

    def __init__(self, responses: dict[str, list[dict]] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, int, float]] = []

    def research_search(self, query: str, *, top_k: int = 10, threshold: float = 0.3,
                        knowledge_id: str = "1_5_520_v2"):
        self.calls.append((query, top_k, threshold))
        return self.responses.get(query, []), None

    def close(self) -> None:
        pass


def test_search_docs_reports_raw_vs_deduped() -> None:
    client = _FakeClient({"300750": [RAW_A, RAW_B, RAW_C]})
    result = search_docs(client, "300750", top_k=20, threshold=0.05)
    assert result["raw_count"] == 3
    assert result["doc_count"] == 2
    assert result["keyword"] == "300750"
    assert result["threshold"] == 0.05
    assert client.calls == [("300750", 20, 0.05)]


def test_search_docs_applies_local_threshold() -> None:
    client = _FakeClient({"300750": [RAW_A, RAW_B]})
    assert search_docs(client, "300750", threshold=0.3)["doc_count"] == 2
    assert search_docs(client, "300750", threshold=0.6)["doc_count"] == 1
    assert search_docs(client, "300750", threshold=0.99)["doc_count"] == 0


def test_collect_notes_one_row_per_symbol() -> None:
    client = _FakeClient({"300750": [RAW_A], "000651": [RAW_B]})
    symbols = [{"symbol": "300750.SZ", "name": "宁德时代"},
               {"symbol": "000651.SZ", "name": "格力电器"}]
    rows = collect_notes(client, symbols, top_k=10, threshold=0.3, source_date="2026-11-19")
    assert [r["symbol"] for r in rows] == ["300750.SZ", "000651.SZ"]
    assert all(r["source_date"] == "2026-11-19" for r in rows)
    assert [c[0] for c in client.calls] == ["300750", "000651"], "检索词用代码"


def test_collect_notes_skips_empty_and_failing_symbols() -> None:
    class _Boom(_FakeClient):
        def research_search(self, query, **kwargs):
            if query == "000651":
                raise RuntimeError("网络炸了")
            return super().research_search(query, **kwargs)

    client = _Boom({"300750": [RAW_A], "601899": []})
    symbols = [{"symbol": "300750.SZ", "name": "宁德时代"},
               {"symbol": "000651.SZ", "name": "格力电器"},
               {"symbol": "601899.SH", "name": "紫金矿业"}]
    rows = collect_notes(client, symbols, source_date="2026-11-19")
    assert [r["symbol"] for r in rows] == ["300750.SZ"], "失败与空命中都不落行"


def test_fetch_rows_reads_adapter_and_requires_api_key(monkeypatch) -> None:
    import app.plugins.pingan.provider as provider_mod
    import app.services.research_notes as mod

    cfg = preset()
    monkeypatch.setattr(provider_mod, "get_api_key", lambda: "")
    with pytest.raises(ValueError, match="API Key"):
        asyncio.run(mod.fetch_rows(cfg, DAY, Path("data")))

    monkeypatch.setattr(provider_mod, "get_api_key", lambda: "ak_test")
    monkeypatch.setattr(mod, "tracked_symbols", lambda data_dir, override=None: [
        {"symbol": "300750.SZ", "name": "宁德时代"}
    ])
    fake = _FakeClient({"300750": [RAW_A]})

    class _ClientFactory:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def research_search(self, query, **kwargs):
            return fake.research_search(query, **kwargs)

        def close(self) -> None:
            pass

    import app.plugins.pingan.client as client_mod
    monkeypatch.setattr(client_mod, "PingAnClient", _ClientFactory)
    rows = asyncio.run(mod.fetch_rows(cfg, DAY, Path("data")))
    assert len(rows) == 1
    assert rows[0]["symbol"] == "300750.SZ"
    assert rows[0]["source_date"] == "2026-11-19"


def test_fetch_rows_raises_when_nothing_found(monkeypatch) -> None:
    import app.plugins.pingan.provider as provider_mod
    import app.services.research_notes as mod

    monkeypatch.setattr(provider_mod, "get_api_key", lambda: "ak_test")
    monkeypatch.setattr(mod, "tracked_symbols", lambda data_dir, override=None: [
        {"symbol": "300750.SZ", "name": "宁德时代"}
    ])
    import app.plugins.pingan.client as client_mod

    class _Empty:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def research_search(self, query, **kwargs):
            return [], None

        def close(self) -> None:
            pass

    monkeypatch.setattr(client_mod, "PingAnClient", _Empty)
    with pytest.raises(ValueError, match="0 行"):
        asyncio.run(mod.fetch_rows(preset(), DAY, Path("data")))


def test_fetch_rows_raises_when_no_symbols(monkeypatch) -> None:
    import app.plugins.pingan.provider as provider_mod
    import app.services.research_notes as mod

    monkeypatch.setattr(provider_mod, "get_api_key", lambda: "ak_test")
    monkeypatch.setattr(mod, "tracked_symbols", lambda data_dir, override=None: [])
    with pytest.raises(ValueError, match="没有可跟踪的标的"):
        asyncio.run(mod.fetch_rows(preset(), DAY, Path("data")))


def test_tracked_symbols_prefers_override_and_dedupes(tmp_path: Path) -> None:
    symbols = tracked_symbols(tmp_path, ["300750.SZ", " 000651.sz ", "300750.SZ"])
    assert [s["symbol"] for s in symbols] == ["000651.SZ", "300750.SZ"]
    assert all(s["name"] == "" for s in symbols), "维表不存在时名字留空, 不影响查询"


def test_tracked_symbols_caps_at_max(tmp_path: Path, monkeypatch) -> None:
    many = [f"{i:06d}.SZ" for i in range(MAX_SYMBOLS + 20)]
    assert len(tracked_symbols(tmp_path, many)) == MAX_SYMBOLS


def test_tracked_symbols_unions_watchlist_and_holdings(tmp_path: Path, monkeypatch) -> None:
    from app.services import manual_holdings, watchlist

    monkeypatch.setattr(watchlist, "list_symbols",
                        lambda: [{"symbol": "300750.SZ"}, {"symbol": "600941.SH"}])
    monkeypatch.setattr(manual_holdings, "load_all",
                        lambda data_dir: {"300750.SZ": {}, "000333.SZ": {}})
    symbols = tracked_symbols(tmp_path)
    assert [s["symbol"] for s in symbols] == ["000333.SZ", "300750.SZ", "600941.SH"]


# ---------------------------------------------------------------------------
# 5. 预设与接线
# ---------------------------------------------------------------------------

def test_preset_recipe() -> None:
    cfg = preset()
    assert cfg.id == CONFIG_ID
    assert cfg.mode == "timeseries"
    assert cfg.pull is not None
    assert cfg.pull.url.endswith("/restapi/yanbao/e-server-transfer-service/dify/retrieval")
    assert cfg.pull.method == "POST"
    assert cfg.pull.response_path == "records"
    assert cfg.pull.auth == {
        "type": "header", "header": "X-API-Key", "key_ref": "pingan_api_key",
    }
    assert cfg.pull.schedule_time == "17:30"
    assert cfg.pull.schedule_period == "daily"
    assert cfg.pull.schedule_trading_only is True
    assert cfg.pull.enabled is True
    adapter = cfg.pull.adapter
    assert adapter is not None
    assert adapter["kind"] == ADAPTER_KIND
    assert adapter["top_k"] == DEFAULT_TOP_K
    assert adapter["threshold"] == DEFAULT_THRESHOLD
    assert adapter["symbols"] == [], "空 = 自动取自选∪持仓"
    assert cfg.code_map == {"type": "computed", "from": "symbol", "method": "strip_exchange"}


def test_preset_has_no_time_field() -> None:
    """每个标每日一行靠 [symbol] 去重; 配 time_field 会把同一标的多篇压成一篇之外的错行。"""
    assert preset().pull.time_field in (None, "")


def test_get_preset_whitelist() -> None:
    assert get_preset(CONFIG_ID) is not None
    assert get_preset("ext_hot_stock") is None


def test_preset_id_does_not_collide_with_builtin_ext_presets() -> None:
    from app.services.ext_presets import _PRESET_IDS

    assert CONFIG_ID not in _PRESET_IDS


def test_adapter_kind_is_registered_in_ext_pull() -> None:
    from app.services.ext_pull import _ADAPTER_KINDS

    assert ADAPTER_KIND in _ADAPTER_KINDS
    assert _ADAPTER_KINDS[ADAPTER_KIND] == ("app.services.research_notes", "fetch_rows")


def test_pull_config_roundtrip_keeps_adapter() -> None:
    cfg = preset()
    restored = PullConfig.from_dict(cfg.pull.to_dict())
    assert restored.adapter == cfg.pull.adapter


def test_ensure_preset_seeds_fresh_install(tmp_path: Path) -> None:
    asyncio.run(ensure_preset(tmp_path))
    config = ExtConfigStore(tmp_path).get(CONFIG_ID)
    assert config is not None
    assert config.pull and config.pull.adapter
    assert (tmp_path / "ext_data" / ".research_note_seeded").exists()


def test_ensure_preset_does_not_overwrite_user_pull(tmp_path: Path) -> None:
    store = ExtConfigStore(tmp_path)
    custom = preset()
    custom.pull.url = "https://example.com/my-own"   # type: ignore[union-attr]
    store.upsert(custom)

    asyncio.run(ensure_preset(tmp_path))
    assert store.get(CONFIG_ID).pull.url == "https://example.com/my-own"  # type: ignore[union-attr]


def test_ensure_preset_fills_factory_shell(tmp_path: Path) -> None:
    store = ExtConfigStore(tmp_path)
    store.upsert(
        ExtConfig(id=CONFIG_ID, label="我自己建的", mode="timeseries",
                  fields=[], pull=PullConfig())
    )
    asyncio.run(ensure_preset(tmp_path))
    cfg = store.get(CONFIG_ID)
    assert cfg is not None
    assert cfg.pull is not None
    assert cfg.pull.adapter and cfg.pull.adapter["kind"] == ADAPTER_KIND
    assert cfg.label == "我自己建的"


def test_ensure_preset_respects_user_deletion(tmp_path: Path) -> None:
    asyncio.run(ensure_preset(tmp_path))
    ExtConfigStore(tmp_path).delete(CONFIG_ID)
    asyncio.run(ensure_preset(tmp_path))
    assert ExtConfigStore(tmp_path).get(CONFIG_ID) is None


def test_ensure_preset_never_raises(tmp_path: Path, monkeypatch) -> None:
    def _boom(self, config):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ExtConfigStore, "upsert", _boom)
    asyncio.run(ensure_preset(tmp_path))


def test_routes_registered() -> None:
    from app.api import research_notes as api_mod

    assert api_mod.router.prefix == "/api/research-notes"
    paths = {r.path for r in api_mod.router.routes}   # type: ignore[attr-defined]
    assert paths == {"/api/research-notes/search", "/api/research-notes/tracked"}


def test_routes_wired_into_app() -> None:
    from app.main import app

    paths = {r.path for r in app.routes}
    assert "/api/research-notes/search" in paths
    assert "/api/research-notes/tracked" in paths


def test_wrap_maps_pingan_errors_to_http() -> None:

    from app.api.research_notes import _wrap
    from app.plugins.pingan.client import (
        PingAnAuthError,
        PingAnError,
        PingAnQuotaError,
        PingAnRateLimitError,
    )

    assert _wrap(ValueError("坏参数")).status_code == 400
    assert _wrap(PingAnAuthError("key")).status_code == 400
    assert _wrap(PingAnQuotaError("quota")).status_code == 429
    assert _wrap(PingAnRateLimitError("rate")).status_code == 429
    assert _wrap(PingAnError("boom")).status_code == 502
    fallback = _wrap(RuntimeError("?"))
    assert fallback.status_code == 500
    assert fallback.detail == "研报检索失败: ?"


def test_search_route_signature_defaults() -> None:
    """路由默认值必须来自服务层常量, 不能各写一份魔数。"""
    import inspect

    from app.api.research_notes import search_notes

    params = inspect.signature(search_notes).parameters
    assert params["top_k"].default.default == DEFAULT_TOP_K
    assert params["threshold"].default.default == DEFAULT_THRESHOLD
    assert MAX_TOP_K == 50
    assert MAX_SYMBOLS >= 50, "跟踪集合上限要能装下自选+持仓"


def test_module_is_importable_without_side_effects() -> None:
    assert "app.services.research_notes" in sys.modules

def test_name_lookup_merges_etf_dimension(tmp_path: Path) -> None:
    """ETF 名称只在 instruments_etf 里, 只读 instruments 会让 ETF 行 name 为空。"""
    import polars as pl

    from app.services.research_notes import name_lookup

    (tmp_path / "instruments").mkdir(parents=True)
    pl.DataFrame({"symbol": ["600941.SH"], "name": ["中国移动"]}).write_parquet(
        tmp_path / "instruments" / "instruments.parquet")
    (tmp_path / "instruments_etf").mkdir(parents=True)
    pl.DataFrame({"symbol": ["159682.SZ"], "name": ["创业板50ETF"]}).write_parquet(
        tmp_path / "instruments_etf" / "instruments_etf.parquet")

    names = name_lookup(tmp_path)
    assert names == {"600941.SH": "中国移动", "159682.SZ": "创业板50ETF"}


def test_name_lookup_survives_missing_tables(tmp_path: Path) -> None:
    from app.services.research_notes import name_lookup

    assert name_lookup(tmp_path) == {}
