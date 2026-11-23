"""Authorization 头的 auth-scheme 必须大小写不敏感 (RFC 7235 §2.1)。

回归背景 (2026-11-12): 外部调用方用 `bearer` / `BEARER` 发送 Token, 服务端只认
字面量 `Bearer ` 前缀, 于是整个请求掉进密码会话分支并返回
**401「未登录或会话已过期」** —— 错误信息指向"没登录", 完全掩盖了真实原因
(其实是认证方案名大小写不匹配)。同一份 Token 用标准写法就是 200。

RFC 7235: auth-scheme 是 case-insensitive; 多余的空白也该容忍。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.services import api_tokens

TOKEN = "tsp_" + "a" * 64


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """带一个有效 Token 的最小应用; 数据目录指向 tmp。"""
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    path = tmp_path / "user_data" / "api_tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"tokens": [{"id": "tok_test", "name": "t", "token_hash": "'
        f'{api_tokens._hash(TOKEN)}",'
        ' "scopes": ["read:market"], "created_at": "2026-11-12T00:00:00+08:00",'
        ' "last_used_at": null, "revoked": false}]}',
        encoding="utf-8",
    )
    api_tokens._last_persist.clear()
    # 必须有密码, 否则「未设密码 + 本机」分支会直接放行, 401 根本不会出现
    (tmp_path / "user_data" / "auth.json").write_text(
        '{"password_hash": "x", "salt": "y", "version": 1}', encoding="utf-8"
    )
    from app.main import app

    return TestClient(app)


def _call(client, authorization: str):
    return client.get("/api/watchlist", headers={"Authorization": authorization})


def _open_network_gate(monkeypatch) -> None:
    """TestClient 的 peer 是 `testserver` (非内网), 会被网络门禁先拦成 403。

    要验证认证语义时先放开这道门, 否则拿到的是网络层的 403 而不是 401。
    """
    from app.services import api_gateway

    monkeypatch.setattr(api_gateway, "token_network_verdict", lambda ip: None)


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
def test_auth_scheme_is_case_insensitive(client, scheme) -> None:
    """认证方案名的大小写不该影响结果。"""
    r = _call(client, f"{scheme} {TOKEN}")

    # 通过认证后才会走到业务层; 只要不是 401「未登录」就说明头被识别成 Token 通道
    assert r.status_code != 401, r.text


def test_extra_whitespace_is_tolerated(client) -> None:
    """`Bearer  <token>` (多空格) 同样合法。"""
    r = _call(client, f"Bearer   {TOKEN}")

    assert r.status_code != 401, r.text


def test_missing_scheme_still_rejected(client, monkeypatch) -> None:
    """没写方案名 (只给 token) 仍按未认证处理 —— 不得放宽成"裸 token 也认"。"""
    _open_network_gate(monkeypatch)
    r = _call(client, TOKEN)

    assert r.status_code == 401


def test_wrong_scheme_still_rejected(client, monkeypatch) -> None:
    """别的方案名 (Basic) 不得被当成 Bearer。"""
    _open_network_gate(monkeypatch)
    r = _call(client, f"Basic {TOKEN}")

    assert r.status_code == 401


def test_invalid_token_reports_token_error_not_session_error(client, monkeypatch) -> None:
    """Token 格式对但值错 → 401 且提示指向 Token, 而不是"未登录"。

    TestClient 的 peer 是 `testserver` (非内网), 会被网络门禁先拦成 403;
    这条要验证的是**认证语义**, 所以显式放开门禁, 别让网络层掩盖真实分支。
    """
    from app.services import api_gateway

    monkeypatch.setattr(api_gateway, "token_network_verdict", lambda ip: None)
    r = _call(client, "Bearer tsp_" + "b" * 64)

    assert r.status_code == 401
    assert "Token" in r.json()["detail"]
