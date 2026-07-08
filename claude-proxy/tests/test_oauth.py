"""T6.4 — oauth（订阅凭证）认证模式：凭证读取 + 身份注入 + 头构造。"""
import json
import time

import pytest

from local_proxy import (
    SmartProxy,
    OAuthCredentialError,
    load_oauth_token,
    CLAUDE_CODE_SYSTEM,
)


# ============================ load_oauth_token ============================

def _write_creds(path, *, access_token="tok-abc", expires_at=None):
    if expires_at is None:
        expires_at = (time.time() + 3600) * 1000  # 默认 1h 后过期
    obj = {"claudeAiOauth": {"accessToken": access_token, "expiresAt": expires_at}}
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


def test_load_token_ok(tmp_path):
    p = _write_creds(tmp_path / ".credentials.json", access_token="tok-xyz")
    assert load_oauth_token(p) == "tok-xyz"


def test_load_token_missing_file(tmp_path):
    with pytest.raises(OAuthCredentialError):
        load_oauth_token(str(tmp_path / "nope.json"))


def test_load_token_malformed_json(tmp_path):
    p = tmp_path / ".credentials.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(OAuthCredentialError):
        load_oauth_token(str(p))


def test_load_token_missing_access_token(tmp_path):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {"expiresAt": (time.time() + 3600) * 1000}}),
                 encoding="utf-8")
    with pytest.raises(OAuthCredentialError):
        load_oauth_token(str(p))


def test_load_token_expired(tmp_path):
    p = _write_creds(tmp_path / ".credentials.json", expires_at=(time.time() - 3600) * 1000)
    with pytest.raises(OAuthCredentialError):
        load_oauth_token(p)


def test_load_token_no_expiry_field_passes(tmp_path):
    # expiresAt 缺失 → 不判过期（容忍未来凭证格式变化），只要有 accessToken 即放行
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-noexp"}}), encoding="utf-8")
    assert load_oauth_token(str(p)) == "tok-noexp"


# ===================== _inject_claude_code_system =====================

def inject(data):
    SmartProxy._inject_claude_code_system(None, data)  # 不使用 self，传 None
    return data


CC_BLOCK = {"type": "text", "text": CLAUDE_CODE_SYSTEM}


def test_inject_when_absent():
    assert inject({})["system"] == [CC_BLOCK]


def test_inject_from_string():
    out = inject({"system": "be terse"})["system"]
    assert out == [CC_BLOCK, {"type": "text", "text": "be terse"}]


def test_inject_prepends_to_list():
    orig = [{"type": "text", "text": "existing"}]
    out = inject({"system": list(orig)})["system"]
    assert out == [CC_BLOCK] + orig


def test_inject_idempotent_when_already_first():
    already = [CC_BLOCK, {"type": "text", "text": "more"}]
    out = inject({"system": list(already)})["system"]
    assert out == already  # 已是身份块开头 → 不重复注入


def test_inject_empty_list():
    assert inject({"system": []})["system"] == [CC_BLOCK]


# ============================ _build_headers ============================

def build(config):
    return SmartProxy._build_headers(None, config)  # 不使用 self，传 None


def test_headers_api_key_mode():
    h = build({"api_key": "sk-deepseek"})  # 缺省 auth_mode=api_key
    assert h["Authorization"] == "Bearer sk-deepseek"
    assert h["Accept-Encoding"] == "identity"
    assert "anthropic-beta" not in h          # api_key 模式不带 oauth 头
    assert "anthropic-version" not in h


def test_headers_oauth_mode(tmp_path):
    p = _write_creds(tmp_path / ".credentials.json", access_token="tok-oauth")
    h = build({"auth_mode": "oauth", "credentials_path": p})
    assert h["Authorization"] == "Bearer tok-oauth"
    assert h["anthropic-version"]              # 官方必需
    assert h["anthropic-beta"]                 # oauth-2025-04-20
    assert "Bearer None" not in h["Authorization"]


def test_headers_oauth_expired_raises(tmp_path):
    p = _write_creds(tmp_path / ".credentials.json", expires_at=(time.time() - 10) * 1000)
    with pytest.raises(OAuthCredentialError):
        build({"auth_mode": "oauth", "credentials_path": p})
