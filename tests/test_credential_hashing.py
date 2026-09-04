"""凭据哈希升级补充测试。

在 `tests/test_security_regressions.py` 已覆盖 argon2id/bcrypt 校验、legacy_sha256 拒绝、
错误口令的基础上，本文件专注任务要求的少数遗漏点：
1. `_verify_credential` 边界：非法/不匹配算法 fail-closed、非 PHC 存储拒绝、空/None 存储拒绝。
2. **审计脱敏**：`audit_security_denial` 与 `issue_login_token` 失败路径绝不记录
   credential/哈希/口令原文；`token_fingerprint` 不泄露令牌原文。
3. `parse_login_credentials` 对非法格式 fail-closed（返回空表）。
4. `issue_login_token` 的 fail-closed 情形：未配置凭据表(503)/凭据未知(401)/成员被拒(403)。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from src.auth.security import (
    _redact_detail,
    _verify_credential,
    audit_security_denial,
    issue_login_token,
    parse_login_credentials,
    token_fingerprint,
)
from src.config import Settings
from src.core.types import DomainError, Role
from src.infrastructure.store import MemoryStore


# ---------------------------------------------------------------------------
# 1. _verify_credential 边界
# ---------------------------------------------------------------------------
def _argon_phc(pw: str) -> str:
    from argon2 import PasswordHasher
    return PasswordHasher().hash(pw)


def _bcrypt_phc(pw: str) -> str:
    import bcrypt
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode("utf-8")


def test_verify_argon2id_correct_and_wrong():
    phc = _argon_phc("correct-pw")
    assert _verify_credential(phc, "correct-pw", "argon2id") == (True, "")
    assert _verify_credential(phc, "wrong-pw", "argon2id") == (False, "login_failed")


def test_verify_argon2id_rejects_bcrypt_stored_value():
    # 配置 argon2id，但存储的是 bcrypt PHC（不猜测、不降级）→ fail-closed。
    bc = _bcrypt_phc("pw")
    assert _verify_credential(bc, "pw", "argon2id") == (False, "login_failed")


def test_verify_bcrypt_correct_and_wrong():
    bc = _bcrypt_phc("correct-pw")
    assert _verify_credential(bc, "correct-pw", "bcrypt") == (True, "")
    assert _verify_credential(bc, "wrong-pw", "bcrypt") == (False, "login_failed")


def test_verify_bcrypt_rejects_argon2_stored_value():
    phc = _argon_phc("pw")
    assert _verify_credential(phc, "pw", "bcrypt") == (False, "login_failed")


def test_verify_legacy_sha256_and_md5_always_rejected():
    sha = hashlib.sha256(b"pw").hexdigest()
    md5 = hashlib.md5(b"pw").hexdigest()
    # 无论配置哪种算法，弱哈希一律拒绝并标注 legacy。
    for algo in ("argon2id", "bcrypt"):
        assert _verify_credential(sha, "pw", algo) == (False, "legacy_sha256")
        assert _verify_credential(md5, "pw", algo) == (False, "legacy_md5")


def test_verify_unknown_algorithm_fails_closed():
    phc = _argon_phc("pw")
    # 未知算法 → 拒绝，绝不用任何回退/弱路径校验。
    assert _verify_credential(phc, "pw", "sha256") == (False, "login_failed")
    assert _verify_credential(phc, "pw", "md5") == (False, "login_failed")
    assert _verify_credential(phc, "pw", "weak") == (False, "login_failed")


def test_verify_empty_algo_defaults_to_argon2id():
    phc = _argon_phc("pw")
    # 未显式配置算法（空串）→ 归一为默认 argon2id（安全默认算法，而非拒绝）。
    assert _verify_credential(phc, "pw", "") == (True, "")
    assert _verify_credential(phc, "wrong", "") == (False, "login_failed")


def test_verify_empty_or_none_stored_fails_closed():
    # 空/None 存储不匹配弱哈希，也不以 PHC 前缀开头 → login_failed。
    assert _verify_credential("", "pw", "argon2id") == (False, "login_failed")
    assert _verify_credential(None, "pw", "argon2id") == (False, "login_failed")
    assert _verify_credential("plain-pw", "plain-pw", "argon2id") == (False, "login_failed")


def test_verify_never_echoes_credential_in_reason():
    # 返回的 reason 只能是固定枚举，绝不包含 credential/哈希内容。
    ok, reason = _verify_credential(_argon_phc("secret-x"), "wrong-x", "argon2id")
    assert reason == "login_failed" and "secret-x" not in repr(reason)


# ---------------------------------------------------------------------------
# 2. 审计脱敏（不泄 credential/hash/令牌）
# ---------------------------------------------------------------------------
def _store() -> MemoryStore:
    s = MemoryStore()
    s.create_tenant("T", "t")
    s.add_membership("T", "U", Role.CUSTOMER)
    return s


def test_redact_detail_replaces_sensitive_keys():
    detail = {
        "credential": "correct-pw",
        "password": "s3cr3t",
        "authorization": "Bearer xyz",
        "token": "raw.token.value",
        "secret": "hmac-key",
        "address": "幸福路1号",
        "card": "622202",
        "payment": "200",
        "phone": "13800138000",
        "email": "a@b.com",
        "algorithm": "argon2id",  # 非敏感，保留
        "reason": "login_failed",  # 非敏感，保留
    }
    out = _redact_detail(detail)
    assert out["credential"] == "[REDACTED]"
    assert out["password"] == "[REDACTED]"
    assert out["authorization"] == "[REDACTED]"
    assert out["token"] == "[REDACTED]"
    assert out["secret"] == "[REDACTED]"
    assert out["address"] == "[REDACTED]"
    assert out["card"] == "[REDACTED]"
    assert out["payment"] == "[REDACTED]"
    assert out["phone"] == "[REDACTED]"
    assert out["email"] == "[REDACTED]"
    assert out["algorithm"] == "argon2id"
    assert out["reason"] == "login_failed"


def test_audit_denial_never_records_credential_or_hash():
    store = _store()
    audit_security_denial(store, "T", "U", "login_failed", "auth", "T:U",
                          {"credential": "correct-pw", "password_hash": "$argon2id$...",
                           "algorithm": "argon2id"})
    rec = store.list_audit("T")[0]
    # 敏感键即脱敏；算法保留。
    assert rec.detail["credential"] == "[REDACTED]"
    assert rec.detail["password_hash"] == "[REDACTED]"
    assert rec.detail["algorithm"] == "argon2id"
    # 全量日志序列化后不含口令/哈希原文。
    dumped = json.dumps({"detail": rec.detail, "user_id": rec.user_id, "target_id": rec.target_id})
    assert "correct-pw" not in dumped
    assert "$argon2id" not in dumped


def test_audit_denial_awal_redacts_key_containing_sensitive_substring():
    # 键名只要包含敏感子串（如 password/credential/secret/token/address）→ 一律脱敏。
    store = _store()
    audit_security_denial(store, "T", "U", "reason_x", "auth", "T:U",
                          {"client_password": "x", "my_credential_secret": "y", "safe_field": 1})
    rec = store.list_audit("T")[0]
    assert rec.detail["client_password"] == "[REDACTED]"
    assert rec.detail["my_credential_secret"] == "[REDACTED]"
    assert rec.detail["safe_field"] == 1


def test_issue_login_token_failure_audit_omits_credential():
    store = _store()
    from argon2 import PasswordHasher
    phc = PasswordHasher().hash("correct-pw")
    settings = Settings(env="production", auth_backend="real", auth_jwt_secret="sec",
                        auth_login_credentials=json.dumps({"T:U": phc}),
                        auth_credential_hash="argon2id")
    with pytest.raises(DomainError):
        issue_login_token(store, settings, "T", "U", "wrong-pw")
    actions = [r.action for r in store.list_audit("T")]
    assert any(a.startswith("security.deny.login_failed") for a in actions)
    # 审计 detail 只含 algorithm，不带 credential/哈希原文。
    assert all("correct-pw" not in json.dumps(r.detail) for r in store.list_audit("T"))
    assert all("wrong-pw" not in json.dumps(r.detail) for r in store.list_audit("T"))


def test_token_fingerprint_never_echoes_token():
    tok = "mock:T:U:customer:deadbeef"
    fp = token_fingerprint(tok)
    assert fp.startswith("token:") and len(fp.split(":")[-1]) == 16
    assert "deadbeef" not in fp and tok not in fp


# ---------------------------------------------------------------------------
# 3. parse_login_credentials fail-closed
# ---------------------------------------------------------------------------
def _creds_settings(raw: str) -> Settings:
    return Settings(env="production", auth_backend="real", auth_jwt_secret="sec",
                    auth_login_credentials=raw)


def test_parse_login_credentials_empty_or_invalid_returns_empty():
    assert parse_login_credentials(_creds_settings("")) == {}
    assert parse_login_credentials(_creds_settings("   ")) == {}
    assert parse_login_credentials(_creds_settings("not-json")) == {}
    assert parse_login_credentials(_creds_settings("[]")) == {}  # 非 dict
    assert parse_login_credentials(_creds_settings('"str"')) == {}


def test_parse_login_credentials_valid_returns_map():
    out = parse_login_credentials(_creds_settings('{"T:U": "$argon2id$v=19$..."}'))
    assert out == {"T:U": "$argon2id$v=19$..."}


# ---------------------------------------------------------------------------
# 4. issue_login_token fail-closed 情形
# ---------------------------------------------------------------------------
def test_issue_login_token_backend_not_real_fails_closed():
    store = _store()
    settings = Settings(env="development", auth_backend="mock", auth_login_credentials="{}")
    with pytest.raises(DomainError) as ei:
        issue_login_token(store, settings, "T", "U", "pw")
    assert ei.value.code == "auth_backend_disabled" and ei.value.status_code == 503


def test_issue_login_token_no_credentials_table_fails_closed():
    store = _store()
    settings = Settings(env="production", auth_backend="real", auth_jwt_secret="sec",
                        auth_login_credentials="")
    with pytest.raises(DomainError) as ei:
        issue_login_token(store, settings, "T", "U", "pw")
    assert ei.value.code == "auth_backend_disabled" and ei.value.status_code == 503


def test_issue_login_token_unknown_credential_user_fails_closed():
    store = _store()
    from argon2 import PasswordHasher
    phc = PasswordHasher().hash("pw1")
    settings = Settings(env="production", auth_backend="real", auth_jwt_secret="sec",
                        auth_login_credentials=json.dumps({"T:U": phc}))
    # U 的凭据表条目缺失（表中只有 U，但登录 U 用错... 实际上表中就是 U）。这里模拟凭据未知：
    # 用不曾在凭据表中出现的第二个用户。
    store.add_membership("T", "U2", Role.CUSTOMER)
    with pytest.raises(DomainError) as ei:
        issue_login_token(store, settings, "T", "U2", "pw1")
    assert ei.value.code == "unauthorized" and ei.value.status_code == 401


def test_issue_login_token_membership_rejected_fails_closed():
    store = _store()
    from argon2 import PasswordHasher
    phc = PasswordHasher().hash("pw1")
    settings = Settings(env="production", auth_backend="real", auth_jwt_secret="sec",
                        auth_login_credentials=json.dumps({"T:U": phc}))
    # U 在租户 T 的成员被撤销 → require_active_membership 抛 403 → 登录失败（不泄露具体原因）。
    store.revoke_membership("T", "U")
    with pytest.raises(DomainError) as ei:
        issue_login_token(store, settings, "T", "U", "pw1")
    assert ei.value.code == "forbidden" and ei.value.status_code == 403
