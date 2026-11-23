"""平安证券 skill 平台 HTTP 客户端。

职责: 认证头、统一信封解包、批量分片与限频重试。不知道 provider / services 层。

协议 (实测 2026-11, 依据平台下发的 pa-market-query skill 包):
  POST https://ai.stock.pingan.com/restapi/hangqing/adapter/hangQing/stockDynamic
  headers: X-API-Key / X-Skill-ID: market-query / X-Request-ID / requestId
           / channelId: c_lite_adapgaty_a_skill
  body:   {"codes": ["SZ159682", "SH510300"]}
  信封:   {"code": 0, "message": "成功", "data": {"items": [...], "not_found": [...]}}

错误语义 (决定上层是重试还是放弃):
  - HTTP 429 / error_code=RATE_LIMIT_EXCEEDED → PingAnRateLimitError (可退避重试)
  - error_code=DAILY_QUOTA_EXCEEDED            → PingAnQuotaError (当日额度用尽, 不重试)
  - HTTP 401/403                               → PingAnAuthError (Key 问题, 不重试)
  - 其他非 0 code / 网络异常                    → PingAnError

直连不走代理: 平安是国内站点, 本机 HTTP(S)_PROXY 会让请求绕远甚至失败
(交接文档「平安接口调用」要求 ProxyHandler({}))。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from typing import TypeVar

logger = logging.getLogger(__name__)

BASE_URL = "https://ai.stock.pingan.com"
SKILL_PREFIX = "restapi/hangqing"
SKILL_ID = "market-query"
CHANNEL_ID = "c_lite_adapgaty_a_skill"
QUOTE_PATH = "adapter/hangQing/stockDynamic"

# ETF 筛选 (jinku 金库通用查询): 路径不带 hangqing 前缀, skill-id 也不同。
# 信封是 {responseCode, currentDate, total, rows} —— 与行情接口的
# {code, message, data} 完全不同, 所以单独走 _post_raw 不平铺 data。
ETF_FILTER_SKILL_ID = "etf-filter"
ETF_FILTER_PATH = "restapi/jinku/omm/v2/http/mop/vault/query"
# pageSize 服务端硬上限 100 (文档「pageSize 最大100」); 无页码参数,
# 全市场 1721 只靠 code 游标翻页 (见 services/etf_valuation.py)。
ETF_FILTER_PAGE_SIZE_MAX = 100

# 热股风向标 (skill=hot-stock)。信封是 `{status, errmsg, results}`, 成功值 status==1。
HOT_STOCK_SKILL_ID = "hot-stock"
HOT_STOCK_PATH = "restapi/hotstock/isspContentService/hotStock/list"
HOT_STOCK_MAX_SIZE = 100

# 研报检索 (skill=research-report)。信封是 `{records, msg}`, HTTP 200 即成功;
# **额外要求 `Authorization: Bearer <平台内置 token>`** —— 不加这个头直接 HTTP 500
# (技能包 scripts/research_report.py 里写死的常量, 不是用户 API Key)。
# 另注意 knowledge_id 写错也是 HTTP 500 (文档声称是 200 + msg 非空, 实测不符)。
RESEARCH_SKILL_ID = "research-report"
RESEARCH_PATH = "restapi/yanbao/e-server-transfer-service/dify/retrieval"
RESEARCH_AUTH_TOKEN = "e4c34022e6a64e0d83dfea24b1540415"
RESEARCH_KNOWLEDGE_ID = "1_5_520_v2"
# top_k 服务端上限 50 (传更大只会返回 50 条以内)。
RESEARCH_MAX_TOP_K = 50

# 平台文档写「最多 5 个代码」; 实测 8 个也能返回 8 条, 但额度是硬约束
# (2000/日), 按文档小批量走, 避免依赖未公开的更大上限。
MAX_CODES_PER_CALL = 5
# 实测 0.15s 间隔连打 12 次会间歇 429; 批次间取更高间隔 + 429 退避。
MIN_INTERVAL_S = 0.35
MAX_RETRIES = 3
_BACKOFF_BASE_S = 1.2

T = TypeVar("T")


class PingAnError(Exception):
    """平安接口错误 (网络失败 / 信封 code != 0 / 结构异常)。"""

    def __init__(self, message: str = "", *, code: int | str | None = None,
                 error_code: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.error_code = error_code


class PingAnAuthError(PingAnError):
    """Key 缺失/无效/无权限 —— 重试无意义。"""


class PingAnRateLimitError(PingAnError):
    """请求过快, 可退避重试。"""


class PingAnQuotaError(PingAnError):
    """当日调用次数用尽, 重试无意义 (次日才恢复)。"""


def chunked(items: list[T], size: int) -> Iterator[list[T]]:
    """按 size 分片, 保持原始顺序。size <= 0 时整体一片。"""
    if size <= 0:
        yield items
        return
    for start in range(0, len(items), size):
        yield items[start:start + size]


class PingAnClient:
    """平安 skill REST 客户端。

    线程安全: 无共享可变状态 (urllib opener 每次请求独立, 节流时间戳由锁保护)。
    """

    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 20.0) -> None:
        if not api_key:
            raise PingAnError("未配置平安 API Key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # 直连: 国内站点, 绕开系统代理
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._last_call = 0.0
        self.last_server_ts = 0  # 预留: 平安响应暂无服务端时间戳字段

    def close(self) -> None:
        """无持久连接需要关闭; 保留接口以对齐 loader 契约。"""
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---- 内部 ----
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < MIN_INTERVAL_S:
            time.sleep(MIN_INTERVAL_S - elapsed)
        self._last_call = time.monotonic()

    def _post(self, path: str, payload: dict) -> dict:
        """POST + 信封解包, 返回 data 字段。失败按类别抛 PingAnError 子类。"""
        obj = self._post_http(f"{SKILL_PREFIX}/{path}", payload, SKILL_ID)

        code = obj.get("code")
        if code not in (0, "0"):
            error_code = str(obj.get("error_code") or "")
            message = str(obj.get("error_message") or obj.get("message") or "")
            if error_code == "RATE_LIMIT_EXCEEDED" or "频繁" in message or "繁忙" in message:
                raise PingAnRateLimitError(f"接口限流: {message or error_code}",
                                           code=code, error_code=error_code)
            if error_code == "DAILY_QUOTA_EXCEEDED":
                raise PingAnQuotaError("今日调用次数已达上限", code=code, error_code=error_code)
            raise PingAnError(f"接口错误 code={code}: {message}", code=code, error_code=error_code)
        data = obj.get("data")
        if not isinstance(data, dict):
            raise PingAnError("响应缺少 data 对象")
        return data

    def _post_with_retry(self, path: str, payload: dict) -> dict:
        """限流退避重试; 鉴权/额度错误立即上抛。"""
        last: PingAnRateLimitError | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return self._post(path, payload)
            except PingAnRateLimitError as e:
                last = e
                if attempt == MAX_RETRIES - 1:
                    break
                wait = _BACKOFF_BASE_S * (2 ** attempt)
                logger.info("平安接口限流, %.1fs 后重试 (第 %d 次)", wait, attempt + 1)
                time.sleep(wait)
        raise last if last is not None else PingAnError("请求失败")

    def _post_http(
        self, url_path: str, payload: dict, skill_id: str,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        """POST + HTTP 层错误分类 + JSON 解析, 返回原始响应对象。

        不做业务码判断 (各家接口的业务码字段名与成功值都不一样):
        行情是 `code == 0`, 金库是 `responseCode == "0"`, 热股是 `status == 1`。
        """
        url = f"{self._base_url}/{url_path}"
        request_id = str(uuid.uuid4())
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-API-Key", self._api_key)
        req.add_header("X-Skill-ID", skill_id)
        req.add_header("X-Request-ID", request_id)
        req.add_header("requestId", request_id)
        req.add_header("channelId", CHANNEL_ID)
        for name, value in (extra_headers or {}).items():
            req.add_header(name, value)

        self._throttle()
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            status = e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PingAnError(f"网络请求失败: {e}") from e

        obj = _parse_json(raw)
        if status == 401:
            raise PingAnAuthError("缺少或无效的 API Key (HTTP 401)", code=status)
        if status == 403:
            raise PingAnAuthError("API Key 无效或没有权限 (HTTP 403)", code=status)
        if status == 429:
            raise _rate_error(obj, status)
        if status == 503:
            raise PingAnError("服务暂时不可用 (HTTP 503)", code=status)
        if status != 200:
            raise PingAnError(f"HTTP {status}", code=status)
        if obj is None:
            raise PingAnError("响应不是 JSON")
        return obj

    def _post_raw(self, path: str, payload: dict, skill_id: str) -> dict:
        """POST 并返回原始响应对象 (不展开 data)。

        用于 jinku 系列接口 —— 它们的信封是 `{responseCode, ..., rows}`,
        没有 `data` 层, 错误文案在 `responseMsg` 而非 `error_message`。
        错误语义与 _post 对齐: 429/401/403/额度 各自抛对应子类。
        """
        obj = self._post_http(path, payload, skill_id)

        code = obj.get("responseCode")
        if code not in (0, "0"):
            message = str(obj.get("responseMsg") or obj.get("message") or "")
            lowered = message
            if "频繁" in lowered or "繁忙" in lowered or "限流" in lowered:
                raise PingAnRateLimitError(f"接口限流: {message}", code=code)
            if "额度" in lowered or "上限" in lowered:
                raise PingAnQuotaError(f"今日调用次数已达上限: {message}", code=code)
            raise PingAnError(f"接口错误 responseCode={code}: {message}", code=code)
        return obj

    def _post_status(self, path: str, payload: dict, skill_id: str) -> dict:
        """POST 并返回原始响应 (热股风向标信封: `{status, errmsg, results}`)。

        **成功值是 `status == 1`** (不是 0, 也没有 responseCode) —— 这是与行情、
        金库两套信封的第三套约定。非法参数 (如 time_range=3) 不会报错, 而是
        `status=1, total=0, list=[]`, 所以调用方必须自己校验参数取值范围。
        """
        obj = self._post_http(path, payload, skill_id)

        code = obj.get("status")
        if code not in (1, "1"):
            message = str(obj.get("errmsg") or obj.get("message") or "")
            if "频繁" in message or "繁忙" in message or "限流" in message:
                raise PingAnRateLimitError(f"接口限流: {message}", code=code)
            if "额度" in message or "上限" in message:
                raise PingAnQuotaError(f"今日调用次数已达上限: {message}", code=code)
            raise PingAnError(f"接口错误 status={code}: {message}", code=code)
        return obj

    def _post_raw_with_retry(self, path: str, payload: dict, skill_id: str) -> dict:
        """同 _post_with_retry, 面向 _post_raw。"""
        last: PingAnRateLimitError | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return self._post_raw(path, payload, skill_id)
            except PingAnRateLimitError as e:
                last = e
                if attempt == MAX_RETRIES - 1:
                    break
                wait = _BACKOFF_BASE_S * (2 ** attempt)
                logger.info("平安接口限流, %.1fs 后重试 (第 %d 次)", wait, attempt + 1)
                time.sleep(wait)
        raise last if last is not None else PingAnError("请求失败")

    def _post_status_with_retry(self, path: str, payload: dict, skill_id: str) -> dict:
        """同 _post_with_retry, 面向 _post_status。"""
        last: PingAnRateLimitError | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return self._post_status(path, payload, skill_id)
            except PingAnRateLimitError as e:
                last = e
                if attempt == MAX_RETRIES - 1:
                    break
                wait = _BACKOFF_BASE_S * (2 ** attempt)
                logger.info("平安接口限流, %.1fs 后重试 (第 %d 次)", wait, attempt + 1)
                time.sleep(wait)
        raise last if last is not None else PingAnError("请求失败")

    def hot_stock_list(self, payload: dict) -> tuple[list[dict], dict]:
        """热股榜单。返回 (榜单条目, 元信息 {update_time,total,page,size})。

        payload 必须是 `{module_code, type_code, time_range, size}`:
          module_code: hot_stock(热搜) / hot_select(加自选) / hot_watch(浏览) / hot_surge(飙升)
          type_code:   aStock / etf / block / hkStock / bond
          time_range:  **整数** 1 = 1 小时, 2 = 24 小时 (默认 2); 传 "1d" 这类字符串是 400
          size:        1..100, 榜单深度上限就是 100 (传 101 服务端不报错但只回 100)

        非法 time_range (如 3) 不报错, 返回 `status=1, total=0, list=[]` —— 空榜单与
        「参数错了」在协议上无法区分, 所以取值校验必须由调用方前置。
        """
        obj = self._post_status_with_retry(
            HOT_STOCK_PATH, payload, HOT_STOCK_SKILL_ID
        )
        results = obj.get("results")
        if not isinstance(results, dict):
            return [], {}
        return list(results.get("list") or []), {
            "update_time": str(results.get("updateTime") or ""),
            "total": int(results.get("total") or 0),
            "page": results.get("page"),
            "size": results.get("size"),
        }


    # ---- 对外 ----
    def quote(self, symbols: list[str]) -> tuple[list[dict], int]:
        """实时快照。symbols 必须是平安格式 (`SZ159682`), 由 provider 层转换。

        返回 (items, server_ts)。server_ts 缺失时为 0, 由调用方回退本地时间。
        部分无效代码由响应里的 not_found 体现, 不算失败。
        """
        if not symbols:
            return [], 0
        data = self._post_with_retry(QUOTE_PATH, {"codes": list(symbols)})
        items = data.get("items")
        if not isinstance(items, list):
            raise PingAnError("响应 items 不是数组")
        not_found = data.get("not_found") or []
        if not_found:
            logger.info("平安快照未识别代码 %d 个: %s", len(not_found), list(not_found)[:10])
        return items, self.last_server_ts

    def query_etf_filter(self, payload: dict) -> tuple[list[dict], int, str]:
        """ETF 金库筛选 (key=ETF_FILTER)。

        payload 由调用方拼 `wheres`/`fields`/`orderBy`/`orderType`/`pageSize`;
        这里只补必需的 `key`。返回 (rows, total, 数据日期)。

        total 是「本轮条件命中的总条数」(不是本页条数), 调用方据此判断是否已
        翻到底 —— 服务端没有页码参数, 只能靠字段游标 (见 services/etf_valuation)。
        """
        body = {"key": "ETF_FILTER", **payload}
        obj = self._post_raw_with_retry(ETF_FILTER_PATH, body, ETF_FILTER_SKILL_ID)
        rows = obj.get("rows")
        if not isinstance(rows, list):
            raise PingAnError("响应 rows 不是数组")
        try:
            total = int(obj.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        return rows, total, str(obj.get("currentDate") or "")

    def research_search(
        self, query: str, *, top_k: int = 10, threshold: float = 0.3,
        knowledge_id: str = RESEARCH_KNOWLEDGE_ID,
    ) -> tuple[list[dict], str | None]:
        """研报库检索。返回 (片段列表, msg)。

        契约要点 (实测 2026-11, 与技能包脚本一致):
          - body.retrieval_setting 的字段名是 **`score_threshhold`** (双 h, 服务端拼错
            的历史字段名), 但**服务端并不按它过滤** —— 同一 query 传 0.05 与 0.6 返回
            完全相同的记录集, 所以阈值过滤必须由调用方在本地做。
          - `top_k` 上限 50; 默认 10。
          - 一条 `record` 只有 `content` / `metadata{path,description}` / `score` /
            `title`, **没有机构、研报类型、发布日期字段** —— 这些只能从正文里抽。
          - 同一篇研报会被切成多个片段分别返回 (同 `metadata.path` 里的 `id=`),
            调用方需要按文档去重。
        """
        if not query or not query.strip():
            raise ValueError("检索关键词不能为空")
        try:
            k = int(top_k)
        except (TypeError, ValueError):
            raise ValueError("top_k 必须是整数") from None
        if k < 1 or k > RESEARCH_MAX_TOP_K:
            raise ValueError(f"top_k 取值范围 1..{RESEARCH_MAX_TOP_K}")
        try:
            thr = float(threshold)
        except (TypeError, ValueError):
            raise ValueError("threshold 必须是 0..1 之间的小数") from None
        if not 0.0 <= thr <= 1.0:
            raise ValueError("threshold 必须是 0..1 之间的小数")

        body = {
            "knowledge_id": knowledge_id,
            "query": query.strip(),
            "retrieval_setting": {"top_k": k, "score_threshhold": thr},
        }
        headers = {"Authorization": f"Bearer {RESEARCH_AUTH_TOKEN}"}
        try:
            obj = self._post_http(RESEARCH_PATH, body, RESEARCH_SKILL_ID,
                                  extra_headers=headers)
        except PingAnError as e:
            # 该接口把「knowledge_id 不存在」也实现成 HTTP 500, 与真故障同码。
            message = str(e)
            if "HTTP 500" in message:
                raise PingAnError(
                    f"研报检索失败 (HTTP 500): 通常是 knowledge_id={knowledge_id!r} 不存在",
                    code=500,
                ) from e
            raise
        records = obj.get("records")
        if not isinstance(records, list):
            raise PingAnError("响应 records 不是数组")
        msg = obj.get("msg")
        return records, (str(msg) if msg else None)


def _parse_json(raw: str) -> dict | None:
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _rate_error(payload: dict | None, status: int) -> PingAnError:
    """HTTP 429 按 error_code 细分额度/限流。"""
    error_code = str((payload or {}).get("error_code") or "")
    message = str((payload or {}).get("error_message") or "")
    if error_code == "DAILY_QUOTA_EXCEEDED":
        return PingAnQuotaError("今日调用次数已达上限 (HTTP 429)", code=status,
                                error_code=error_code)
    return PingAnRateLimitError(f"接口限流: {message or error_code or 'HTTP 429'}",
                                code=status, error_code=error_code)
