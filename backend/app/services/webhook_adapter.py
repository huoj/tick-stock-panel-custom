"""Webhook 推送适配器 — 把告警事件推送到外部 IM / 量化软件。

职责: 把后端产生的告警事件, 通过用户配置的 Webhook 地址推送到外部。
     目前支持飞书、企业微信和通用第三方 JSON Webhook。

飞书自定义机器人接入:
  1. 飞书群 → 群设置 → 群推送 Webhook → 添加「自定义机器人」
  2. 复制生成的 Webhook 地址 (形如 https://open.feishu.cn/open-apis/bot/v2/hook/xxx)
  3. (可选) 安全设置 → 启用「签名校验」, 记录签名密钥(secret)
  4. 填入设置页「飞书 Webhook」配置

设计: 失败静默降级, 绝不因推送失败阻断告警主流程 (落盘 / SSE 推送)。
     去重不在本层做, 复用 MonitorRuleEngine 的 cooldown。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 单次推送最长字符 (飞书单条文本消息上限 30KB, 这里保守截断避免刷屏)
_MAX_LEN = 500

# 卡片消息正文最长字符 (飞书 interactive 卡片上限 30KB, 保守留余量给标题/结构)
_CARD_MAX_LEN = 28000

# 企业微信群推送 Webhook markdown 消息上限 4096 字节 (非字符; 中文每字 3 字节),
# 留余量给标题、格式符及截断提示行。
_WECOM_MD_MAX_BYTES = 4000

# 截断提示行: 正文超长被截断时追加, 引导用户回应用内查看完整内容。
_WECOM_TRUNCATED_HINT = "\n\n…内容较长已截断，更多详情请回到 TickFlow 应用内查看。"

# 飞书自定义机器人 Webhook 前缀 (用于 URL 合法性校验)
FEISHU_HOOK_PREFIX = "https://open.feishu.cn/open-apis/bot/v2/hook/"


def _truncate(text: str) -> str:
    """截断超长文本。"""
    text = (text or "").strip()
    return text[:_MAX_LEN] + ("…" if len(text) > _MAX_LEN else "")


def is_valid_feishu_url(url: str) -> bool:
    """校验是否为合法的飞书自定义机器人 Webhook 地址。"""
    return bool(url) and url.startswith(FEISHU_HOOK_PREFIX)


def _gen_sign(timestamp: str, secret: str) -> str:
    """计算飞书自定义机器人签名。

    算法 (官方): 把 `timestamp + "\\n" + secret` 作为签名字符串 (key),
    用 HmacSHA256 计算空字符串的签名结果, 再 Base64 编码。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _truncate_card(text: str) -> str:
    """截断卡片正文 (留余量给标题与卡片结构)。"""
    text = (text or "").strip()
    return text[:_CARD_MAX_LEN] + ("…" if len(text) > _CARD_MAX_LEN else "")


def _truncate_to_bytes(text: str, max_bytes: int, suffix: str = "…") -> str:
    """按 UTF-8 字节数安全截断 (不截断在多字节字符中间, 末尾补 suffix)。

    企业微信 markdown 按**字节**计长 (上限 4096), 中文每字 3 字节,
    不能用字符数截断 (len(str) 算的是字符数, 会超字节上限)。

    Args:
        max_bytes: 含 suffix 在内的总字节上限。
        suffix:    被截断时追加的结尾 (如省略号或提示行)。
    """
    text = (text or "").strip()
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix_bytes = suffix.encode("utf-8")
    # 为 suffix 留出字节, 再用 errors='ignore' 丢弃被截断的尾字节, 避免半个字符
    cut = encoded[:max_bytes - len(suffix_bytes)]
    return cut.decode("utf-8", errors="ignore") + suffix


_FEISHU_MAX_ATTEMPTS = 3


def _post_feishu(webhook_url: str, payload: dict, secret: str, max_attempts: int = _FEISHU_MAX_ATTEMPTS) -> bool:
    """发送飞书 webhook 请求并判定成败 (供 text / card 共用)。

    成功响应: HTTP 200 且业务 code=0 (或非 JSON/非 dict 的 200)。

    瞬时失败 (网络/超时/HTTP 5xx) 会**带退避重试** —— 告警冷却在事件生成时即打戳,
    一次瞬时 5xx/timeout 若不重试, 该告警会被冷却窗口(默认 1h)压掉, 离屏用户彻底
    收不到推送。永久失败 (4xx / 业务 code≠0, 如签名错、URL 失效) 不重试。最终失败
    记 WARNING (而非之前的 debug), 保证「推送丢了」在日志里可见。

    max_attempts: 尝试次数, 默认 3 (生产推送语义)。诊断用途(如手动测试配置)可传 1,
    避免失败时等满退避重试。
    """
    import httpx

    last_err = ""
    for attempt in range(1, max_attempts + 1):
        try:
            # 启用签名校验时, 请求体须带 timestamp + sign (每次重试都重算, 防时间戳过期)
            if secret:
                timestamp = str(int(time.time()))
                payload["timestamp"] = timestamp
                payload["sign"] = _gen_sign(timestamp, secret)

            resp = httpx.post(webhook_url, json=payload, timeout=5.0)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    return True  # 非 JSON 的 200, 视为成功
                if isinstance(data, dict):
                    code = data.get("code", data.get("StatusCode", 0))
                    if code == 0:
                        return True
                    # 业务失败(签名错/格式错等): 重试无益, 直接失败
                    logger.warning("飞书推送业务失败(不重试): %s", data)
                    return False
                return True  # 200 且 JSON 非 dict, 视为成功
            # 4xx 客户端错误(URL 失效等): 不重试; 5xx: 落入重试
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code < 500:
                logger.warning("飞书推送失败(不重试, 客户端错误): %s", last_err)
                return False
        except Exception as e:  # noqa: BLE001 — 网络/超时, 可重试
            last_err = str(e)

        if attempt < max_attempts:
            time.sleep(min(2 ** (attempt - 1), 3))  # 退避: 1s, 2s

    logger.warning("飞书 Webhook 推送最终失败(已重试 %d 次): %s", max_attempts, last_err)
    return False


def send_feishu(webhook_url: str, title: str, body: str, secret: str = "", max_attempts: int = _FEISHU_MAX_ATTEMPTS) -> bool:
    """推送一条文本消息到飞书群推送 Webhook。

    Args:
        webhook_url: 飞书自定义机器人 Webhook 地址
        title:       消息标题 (与正文拼接为一条文本)
        body:        消息正文
        secret:      签名密钥 (机器人启用了「签名校验」时必填; 留空则不带签名)
        max_attempts: 尝试次数 (诊断用途可传 1, 默认保持生产重试语义)

    Returns:
        True=成功送达, False=失败或 URL 非法。
        失败静默, 不抛异常 (Webhook 是辅助通道, 不能阻断告警主流程)。
    """
    if not is_valid_feishu_url(webhook_url):
        return False

    text = _truncate(f"{title}\n{body}".strip())
    if not text:
        return False

    payload: dict = {"msg_type": "text", "content": {"text": text}}
    return _post_feishu(webhook_url, payload, secret, max_attempts)


def send_feishu_card(webhook_url: str, title: str, subtitle: str, body_md: str, secret: str = "") -> bool:
    """推送一条 interactive 卡片消息到飞书群推送 Webhook —— 用 lark_md 渲染完整 markdown 报告。

    飞书「自定义机器人」webhook 不支持文件附件, 但 interactive 卡片的 lark_md 元素
    可渲染 markdown, 能承载完整复盘报告(通常 2-5KB, 远小于卡片 30KB 上限)。

    Args:
        webhook_url: 飞书自定义机器人 Webhook 地址
        title:       卡片标题 (显示在蓝色 header)
        subtitle:    副标题 (加粗显示, 如日期/情绪标签; 留空则省略)
        body_md:     卡片正文 markdown (报告全文)
        secret:      签名密钥 (启用签名校验时必填)

    Returns:
        True=成功送达, False=失败或 URL 非法。
        失败静默, 不抛异常 (与 send_feishu 一致, 不阻断告警主流程)。
    """
    if not is_valid_feishu_url(webhook_url):
        return False

    body = _truncate_card(body_md)
    elements: list[dict] = []
    if subtitle.strip():
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**{subtitle.strip()}**"},
        })
        elements.append({"tag": "hr"})
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": body},
    })

    payload: dict = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": elements,
        },
    }
    return _post_feishu(webhook_url, payload, secret)


# 白话动作映射表 —— 只认 rule_id, 绝不从 message 自由文本猜关键词。
# 血的教训: 曾按 message 关键词匹配, 结果 ll_buy2(买第2批)的消息里顺带写了
# "跌破12.20止损", 被误判成"该止损了"——买点报成止损, 方向完全反了。
# rule_id 是我们自己定义的稳定契约, 消息措辞会变, 但 id 语义不会, 因此可审计、
# 可测试。每条规则必须映射到确定的一句动作; 新增规则时若忘了归类, 测试会拦下。
_RULE_ACTIONS = (
    # (匹配规则, 动作) —— 顺序即优先级, 从上到下第一个命中生效
    # pa_rule_ 必须最前: 其中 etf_deep_value 含 "_deep", 否则会被下面的买入档误吞
    (lambda r: r.startswith("pa_rule_"), "策略信号：仅供参考，结合自身情况判断"),
    (lambda r: r.endswith("_stop"), "该止损了：先减仓，别补仓、别硬扛"),
    (lambda r: r.startswith("ll_dump") or r.endswith("_dump"), "急跌放量：先别接，观察是否止跌"),
    (lambda r: r.startswith("up_"), "涨到减仓档了：考虑分批止盈，别贪最后一个板"),
    (lambda r: r.startswith("br_") or r.endswith("_break"), "放量突破：可关注，追的话用半仓、设好止损"),
    (lambda r: r.startswith("lz_"), "进入左侧观察区：可以关注，但别急着买"),
    (lambda r: "_buy" in r or "_deep" in r, "到买入价位了：按计划分批，别一次满仓"),
    (lambda r: r.startswith("cm_risk"), "跌破风险线：趋势转弱，先别急着接"),
    (lambda r: r.startswith("sec_") or r.startswith("idx_"), "板块或大盘异动：对照自己的持仓，别跟着情绪操作"),
    (lambda r: r.endswith("_drop"), "日内跌幅到位：核对该不该止损，别盲目补仓"),
    (lambda r: r.endswith("_rise"), "日内涨幅较大：拿住或分批止盈，别追高加仓"),
)


def _plain_action(ev: dict) -> str:
    """把一条告警提炼成「现在该做什么」的白话动作。

    只按 rule_id 的命名契约查表, 不读 message —— 见 _RULE_ACTIONS 的教训注释。
    识别不了返回空串(卡片不显示这一行), 不编造、不猜。
    """
    rid = str(ev.get("rule_id") or "").lower()
    for match, action in _RULE_ACTIONS:
        if match(rid):
            return action
    return ""


def _build_alert_card(ev: dict) -> dict:
    """把一条监控告警渲染成 watch.py 档位卡片风格的 interactive 卡片 dict。

    纯渲染函数, 无投递副作用 —— 群 webhook 与单聊共用同一套卡片视觉。
    数据密度对齐 watch_card.build(): 现价色块 + 分钟走势 + 位置标签 +
    多行指标(最高/最低/昨收/成交额/量比/换手/RSI/振幅/距支撑等) + 触发依据 +
    双时间(行情/发送)。缺哪个字段就少显示一项, 不补假值。
    """
    import re

    RED, GREEN, ORANGE, GREY, BLUE = "red", "green", "orange", "grey", "blue"
    severity = str(ev.get("severity") or "info")
    sev_style = {
        "critical": ("P0", "red", RED),
        "warn": ("P1", "orange", ORANGE),
        "info": ("P2", "blue", GREY),
    }.get(severity, ("P2", "blue", GREY))
    badge, template, sev_color = sev_style

    symbol = str(ev.get("symbol") or "")
    name = str(ev.get("name") or "")
    rule_name = str(ev.get("rule_name") or "")
    message = str(ev.get("message") or "").strip()
    q = ev.get("quote") or {}   # monitor.py 补进来的行情快照指标

    # message 已被 _body_with_quote 补过 "· 现价 X · ±Y%"; 这里去掉尾巴,
    # 现价单独用色块展示, 避免同一句里出现两遍。
    message = re.sub(r"\s*·\s*现价\s*[\d.]+\s*(?:·\s*[+-]?[\d.]+%)?\s*$", "", message).strip()

    pct = ev.get("change_pct")   # 小数制: 0.05 = 5%
    price = ev.get("price")
    tone = GREY
    if isinstance(pct, (int, float)):
        tone = RED if pct > 0 else GREEN if pct < 0 else GREY

    def _div(content: str) -> dict:
        return {"tag": "div", "text": {"tag": "lark_md", "content": content}}

    def _cell(text: str, px: int, align: str = "left") -> dict:
        return {"tag": "column", "width": f"{px}px", "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text, "text_align": align}}]}

    def _metric_row(metrics: list[tuple[str, str, str | None]]) -> dict | None:
        """一行最多4项, 列宽 [110,100,100,110], 数值右对齐 (与 watch_card 同款)。"""
        if not metrics:
            return None
        n = len(metrics)
        widths = {4: [110, 100, 100, 110], 3: [140, 100, 120], 2: [200, 200]}
        ws = widths.get(n) or [int(420 / n)] * n
        aligns = ["left"] + ["right"] * (n - 1)
        cols = []
        for (label, value, color), w, a in zip(metrics, ws, aligns):
            body = f"<font color='{color}'>{value}</font>" if color else str(value)
            cols.append(_cell(f"<font color='{GREY}'>{label}</font>\n{body}", w, a))
        return {"tag": "column_set", "flex_mode": "none",
                "horizontal_spacing": "default", "columns": cols}

    def _num(v, fmt="{:.2f}"):
        return isinstance(v, (int, float)) and v == v  # 排除 None / NaN

    els: list[dict] = []
    ident = f"**{name}**（{symbol}）" if symbol else f"**{name}**"
    els.append(_div(f"<font color='{sev_color}'>[{badge}]</font> 🔔 {ident}"))

    # 白话动作结论: 入门者一眼先看到「该做什么」, 指标是佐证。放最上面。
    action = _plain_action(ev)
    if action:
        els.append(_div(f"**<font color='{sev_color}'>▸ {action}</font>**"))
        els.append({"tag": "hr"})

    if rule_name:
        els.append(_div(f"<font color='{GREY}'>{rule_name}</font>"))

    if price is not None:
        els.append({"tag": "hr"})
        seg = [f"**<font color='{tone}'>{float(price):.2f}</font>**"]
        if isinstance(pct, (int, float)):
            seg.append(f"**<font color='{tone}'>{pct * 100:+.2f}%</font>**")
        els.append(_div("　".join(seg)))

    # 分钟走势: 30 个块字符, 说明"怎么走到这里的"。由投递前用 repo 补进 ev["spark"]。
    spark = (ev.get("spark") or "").strip()
    if spark:
        line = f"<font color='{GREY}'>{spark}</font>"
        if ev.get("spark_label"):
            line += f" <font color='{GREY}'>{ev['spark_label']}</font>"
        els.append(_div(line))

    # 位置定性标签 (创 60 日新高/新低 等)
    tags = [t for t in (ev.get("quote_tags") or []) if t]
    if tags:
        els.append(_div("　".join(f"<font color='{ORANGE}'>▸ {t}</font>" for t in tags)))

    # 多行指标 (数据密度主体)
    metrics: list[tuple[str, str, str | None]] = []
    hi, lo = q.get("high"), q.get("low")
    prev, amt = q.get("prev_close"), q.get("amount")
    if _num(hi): metrics.append(("最高", f"{hi:.2f}", None))
    if _num(lo): metrics.append(("最低", f"{lo:.2f}", None))
    if _num(prev): metrics.append(("昨收", f"{prev:.2f}", None))
    if _num(amt): metrics.append(("成交额", f"{amt / 1e8:.2f}亿", None))

    vr = q.get("vol_ratio_5d")
    tr = q.get("turnover_rate")
    rsi = q.get("rsi_6")
    amp = q.get("amplitude")
    vr_tone = (RED if vr >= 1.5 else GREEN if vr < 0.8 else None) if _num(vr) else None
    rsi_tone = (RED if rsi >= 80 else GREEN if rsi <= 20 else None) if _num(rsi) else None
    if _num(vr): metrics.append(("量比", f"{vr:.2f}", vr_tone))
    if _num(tr): metrics.append(("换手", f"{tr:.2f}%", None))
    if _num(rsi): metrics.append(("RSI6", f"{rsi:.1f}", rsi_tone))
    if _num(amp): metrics.append(("振幅", f"{amp * 100:.2f}%", None))

    ld = q.get("limit_down_price")
    if _num(ld) and ld > 0 and _num(price):
        dist = (float(price) / ld - 1) * 100
        if dist <= 5:
            metrics.append(("距跌停", f"{dist:.1f}%", RED if dist <= 2 else None))
    boll_up, boll_low = q.get("boll_upper"), q.get("boll_lower")
    if _num(price) and _num(boll_up) and _num(boll_low) and boll_up > boll_low:
        pos = (float(price) - boll_low) / (boll_up - boll_low)
        boll_pos = "上轨上方" if pos > 1 else "下轨下方" if pos < 0 else f"{pos * 100:.0f}%位"
        metrics.append(("布林", boll_pos, None))

    for i in range(0, len(metrics), 4):
        row = _metric_row(metrics[i:i + 4])
        if row:
            els.append(row)

    if message:
        els.append(_div(f"<font color='{ORANGE}'>▸ {message}</font>"))

    # 双时间: 行情时间 (数据中最后一刻) 与 触发时刻分开, 盘前/停牌时能判断消息新旧。
    # 落款首项固定带来源, 与 watch.py 本地脚本的卡片区分 (两者都进同一个单聊)。
    foot: list[str] = [f"<font color='{BLUE}'>来源 TSP 监控</font>"]
    if ev.get("rule_id"):
        foot.append(f"规则 {ev['rule_id']}")
    ts = ev.get("ts")
    trigger_t = ""
    if ts:
        try:
            import datetime as _dt
            from app.market_time import cn_now

            trigger_t = _dt.datetime.fromtimestamp(float(ts) / 1000, tz=cn_now().tzinfo).strftime("%H:%M:%S")
        except Exception:  # noqa: BLE001
            pass
    if ev.get("quote_time"):
        foot.append(f"行情 {ev['quote_time']}")
    if ev.get("sent_at"):
        foot.append(f"发送 {ev['sent_at']}")
    elif trigger_t:
        foot.append(f"触发 {trigger_t}")
    if foot:
        els.append({"tag": "hr"})
        els.append(_div(f"<font color='{GREY}'>{'　'.join(foot)}</font>"))

    header_title = (f"{name} · {message}".strip(" ·") or symbol or "监控告警")[:60]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": header_title}, "template": template},
        "elements": els,
    }


def send_feishu_alert_card(webhook_url: str, ev: dict, secret: str = "") -> bool:
    """把监控告警以档位卡片推送到飞书群自定义机器人 webhook。"""
    if not is_valid_feishu_url(webhook_url):
        return False
    try:
        card = _build_alert_card(ev)
        return _post_feishu(webhook_url, {"msg_type": "interactive", "card": card}, secret)
    except Exception as e:  # noqa: BLE001
        logger.warning("飞书告警卡片渲染失败, 回退文本: %s", e)
        return send_feishu(
            webhook_url,
            ev.get("rule_name") or "监控告警",
            f"{ev.get('symbol') or ''} {ev.get('message') or ''}".strip(),
            secret,
        )


# ---- 飞书单聊 (Deepseek AI 助手) ----
# 群 webhook 与单聊是两套完全不同的 API:
#   群 = 自定义机器人 webhook + 可选签名; 单聊 = tenant_access_token + receive_id(open_id)。
# 这里把告警卡片直接投递到用户单聊, 免去盯群。
# 所有凭据一律从环境变量读取, 不在源码中硬编码任何 app_id/open_id/密钥/路径。
#   FEISHU_DM_APP_ID      飞书应用 app_id
#   FEISHU_DM_OPEN_ID     接收人 open_id
#   FEISHU_DM_APP_SECRET  应用密钥 (优先; 也可用 FEISHU_DM_CRED_FILE 指向 yaml 凭据文件)
#   FEISHU_DM_CRED_FILE   yaml 凭据文件路径, 配合 FEISHU_DM_SECRET_KEY 指定键名
_FEISHU_DM_APP_ID = os.environ.get("FEISHU_DM_APP_ID", "")
_FEISHU_DM_OPEN_ID = os.environ.get("FEISHU_DM_OPEN_ID", "")
_FEISHU_DM_CRED_FILE = os.environ.get("FEISHU_DM_CRED_FILE", "")
_FEISHU_DM_SECRET_KEY = os.environ.get("FEISHU_DM_SECRET_KEY", "DSH_FEISHU_APP_SECRET")
_feishu_dm_token_cache: dict[str, object] = {"value": None, "expire_at": 0.0}


def _read_feishu_dm_secret() -> str | None:
    # 1) 环境变量直接给密钥
    env_secret = os.environ.get("FEISHU_DM_APP_SECRET", "")
    if env_secret:
        return env_secret
    # 2) 从 yaml 凭据文件读取 (路径与键名均可配置)
    if not _FEISHU_DM_CRED_FILE:
        return None
    try:
        with open(_FEISHU_DM_CRED_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, val = line.partition(":")
                if key.strip() == _FEISHU_DM_SECRET_KEY:
                    return val.strip().strip("'\"")
    except OSError:
        return None
    return None


def is_feishu_dm_configured() -> bool:
    """飞书单聊是否可用 (凭据文件可读即可; 不发起网络请求)。

    单聊不需要群 webhook URL —— 仅靠环境变量配置的 app_id/open_id + 密钥。
    quote_service 的全局渠道早退判断必须把它算进来, 否则「只配单聊、
    没配群地址」时所有告警在早退处被掐死, 单聊永不投递。
    """
    return bool(_read_feishu_dm_secret())


def _get_feishu_dm_token() -> str | None:
    """换 tenant_access_token, 进程内缓存到过期前 2 分钟。"""
    import urllib.request

    now = time.time()
    if _feishu_dm_token_cache["value"] and now < float(_feishu_dm_token_cache["expire_at"]):
        return str(_feishu_dm_token_cache["value"])
    secret = _read_feishu_dm_secret()
    if not secret:
        logger.warning("飞书单聊 secret 未找到 (%s)", _FEISHU_DM_CRED_FILE)
        return None
    try:
        payload = json.dumps({"app_id": _FEISHU_DM_APP_ID, "app_secret": secret}).encode("utf-8")
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=payload,
            method="POST",
        )
        req.add_header("Content-Type", "application/json; charset=utf-8")
        # 直连, 绕开本机代理把 no_proxy 写成 [::1] 导致 httpx 初始化报错的坑
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("飞书单聊 token 请求失败: %s", e)
        return None
    if res.get("code") != 0 or not res.get("tenant_access_token"):
        logger.warning("飞书单聊 token 被拒: %s %s", res.get("code"), res.get("msg"))
        return None
    token = str(res["tenant_access_token"])
    _feishu_dm_token_cache["value"] = token
    _feishu_dm_token_cache["expire_at"] = now + max(int(res.get("expire", 7200)) - 120, 60)
    return token


def send_feishu_dm_card(ev: dict) -> bool:
    """把监控告警以同样的档位卡片直接投递到 Deepseek AI 助手单聊。失败静默。"""
    import urllib.parse
    import urllib.request

    try:
        token = _get_feishu_dm_token()
        if not token:
            return False
        card = _build_alert_card(ev)
        qs = urllib.parse.urlencode({"receive_id_type": "open_id"})
        payload = {
            "receive_id": _FEISHU_DM_OPEN_ID,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        req = urllib.request.Request(
            f"https://open.feishu.cn/open-apis/im/v1/messages?{qs}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Authorization", f"Bearer {token}")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        if res.get("code") != 0:
            logger.warning("飞书单聊告警投递被拒: %s %s", res.get("code"), res.get("msg"))
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("飞书单聊告警投递失败: %s", e)
        return False


# ================================================================
# 企业微信群推送 Webhook
# ================================================================
#
# 与飞书自定义机器人几乎同构: 同样是"群推送 Webhook + POST JSON"。
# 关键差异:
#   1. Webhook 形态: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
#   2. 无需签名校验 (key 本身即凭证; 企业微信群推送 Webhook 可选"签名校验"但极少用)
#   3. Markdown 原生支持 (msgtype=markdown), 不必像飞书那样包进 interactive 卡片
#   4. 成功响应: {"errcode":0,"errmsg":"ok"}
#
# 限制: 每个机器人每分钟最多 20 条消息 (超出会被限流 460min 内不可用),
#      依赖 MonitorRuleEngine 的 cooldown 去重即可应对告警场景。
# 企业微信群的消息可在绑定的个人微信接收, 实现"微信推送"体验。

WECOM_HOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


def is_valid_wecom_url(url: str) -> bool:
    """校验是否为合法的企业微信群推送 Webhook 地址。

    允许两种写法:
      - 完整: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
      - 仅 key: xxx (企业微信群推送 Webhook 的 key 为 36 位 UUID 样式, 保存时自动补全)
    """
    if not url:
        return False
    if url.startswith(WECOM_HOOK_PREFIX):
        return True
    # 纯 key: 企业微信 key 形如 12345678-1234-1234-1234-1234567890ab (36 位),
    # 但用户可能截断, 放宽到 >= 20 位的无空格无斜杠字符串。
    url = url.strip()
    if " " in url or "/" in url or "?" in url:
        return False
    return len(url) >= 20


def normalize_wecom_url(url: str) -> str:
    """把纯 key 补全为完整 Webhook URL。已是完整 URL 则原样返回。"""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith(WECOM_HOOK_PREFIX):
        return url
    return f"{WECOM_HOOK_PREFIX}?key={url}"


def _post_wecom(webhook_url: str, payload: dict) -> bool:
    """发送一次企业微信 webhook 请求并判定成败。

    成功响应: HTTP 200 且 errcode=0。失败静默返回 False。
    """
    try:
        import httpx

        resp = httpx.post(webhook_url, json=payload, timeout=5.0)
        if resp.status_code == 200:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    # errcode=0 表示成功; 45009=频率限制, 其它非零=业务失败
                    if data.get("errcode") == 0:
                        return True
                    logger.warning("企业微信推送业务失败: %s", data)
                    return False
            except ValueError:
                return True
        logger.warning("企业微信推送 HTTP %s: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("企业微信 Webhook 推送失败: %s", e)
        return False


def send_wecom(webhook_url: str, title: str, body: str) -> bool:
    """推送一条文本消息到企业微信群推送 Webhook。

    Args:
        webhook_url: 企业微信群推送 Webhook 地址 (或纯 key, 会自动补全)
        title:       消息标题 (与正文拼接为一条文本)
        body:        消息正文

    Returns:
        True=成功送达, False=失败或 URL 非法。
        失败静默, 不抛异常 (与 send_feishu 一致)。
    """
    webhook_url = normalize_wecom_url(webhook_url)
    if not is_valid_wecom_url(webhook_url):
        return False

    text = _truncate(f"{title}\n{body}".strip())
    if not text:
        return False

    payload: dict = {"msgtype": "text", "text": {"content": text}}
    return _post_wecom(webhook_url, payload)


def send_wecom_markdown(webhook_url: str, title: str, body_md: str) -> bool:
    """推送一条 Markdown 消息到企业微信群推送 Webhook —— 承载完整复盘报告。

    企业微信群推送 Webhook 原生支持 markdown 类型 (比飞书 interactive 卡片简单),
    支持 # ## **粗体** >引用 - 列表 等基础语法, 单条上限 4096 字节。

    Args:
        webhook_url: 企业微信群推送 Webhook 地址 (或纯 key)
        title:       标题 (作为一级标题 ## 拼到正文前)
        body_md:     markdown 正文

    Returns:
        True=成功送达, False=失败或 URL 非法。
    """
    webhook_url = normalize_wecom_url(webhook_url)
    if not is_valid_wecom_url(webhook_url):
        return False

    # 企业微信 markdown 上限 4096 字节 (含标题), 用按字节截断避免超限被拒。
    # 超长时追加提示行, 引导用户回应用内查看完整报告。
    raw = f"## {title}\n\n{body_md}".strip()
    content = _truncate_to_bytes(raw, _WECOM_MD_MAX_BYTES, suffix=_WECOM_TRUNCATED_HINT)
    if not content.strip():
        return False

    payload: dict = {"msgtype": "markdown", "markdown": {"content": content}}
    return _post_wecom(webhook_url, payload)


# ================================================================
# 通用第三方 JSON Webhook
# ================================================================

_CUSTOM_MAX_ATTEMPTS = 3


def is_valid_custom_url(url: str) -> bool:
    """Accept absolute HTTP(S) URLs, including LAN endpoints used by local deployments."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username


def send_custom(
    webhook_url: str,
    title: str,
    body: str,
    event_type: str,
    data: dict | None = None,
    secret: str = "",
    max_attempts: int = _CUSTOM_MAX_ATTEMPTS,
) -> bool:
    """POST a stable JSON envelope to a user-configured third-party system.

    When ``secret`` is configured the raw request body is signed with HMAC-SHA256.
    The receiver can validate ``X-TickFlow-Timestamp`` and
    ``X-TickFlow-Signature: sha256=<hex>`` before accepting the event.
    """
    if not is_valid_custom_url(webhook_url):
        return False

    timestamp = str(int(time.time()))
    payload = {
        "event": str(event_type or "notification"),
        "timestamp": int(timestamp),
        "title": str(title or ""),
        "body": str(body or ""),
        "data": data or {},
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "TickFlow-Webhook/1.0"}
    if secret:
        digest = hmac.new(secret.encode("utf-8"), encoded, hashlib.sha256).hexdigest()
        headers["X-TickFlow-Timestamp"] = timestamp
        headers["X-TickFlow-Signature"] = f"sha256={digest}"

    import httpx

    last_err = ""
    for attempt in range(1, max_attempts + 1):
        try:
            response = httpx.post(
                webhook_url, content=encoded, headers=headers, timeout=5.0,
            )
            if 200 <= response.status_code < 300:
                return True
            last_err = f"HTTP {response.status_code}: {response.text[:200]}"
            if response.status_code < 500:
                logger.warning("第三方 Webhook 推送失败(不重试): %s", last_err)
                return False
        except Exception as exc:  # Network failures are retryable and must not escape.
            last_err = str(exc)
        if attempt < max_attempts:
            time.sleep(min(2 ** (attempt - 1), 3))

    logger.warning("第三方 Webhook 推送最终失败(已重试 %d 次): %s", max_attempts, last_err)
    return False
