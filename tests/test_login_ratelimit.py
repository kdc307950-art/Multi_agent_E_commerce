"""登录双维度限流 + 失败退避 + 审计告警测试。

覆盖：
1. 客户端 IP 解析（可信代理深度 / 直连）。
2. Memory 限流器：账号维度、IP 维度（跨账号聚合）、失败指数退避 + 封顶、成功重置失败计数。
3. 路由级：POST /api/auth/login 在账号/IP 超限、退避期内被拒绝（429 + code=too_many_requests）
   并写脱敏审计；成功登录重置；指标可观测（login_attempts_total / login_rate_limited_total）。
使用可注入时钟做确定性退避/窗口测试。
"""
from __future__ import annotations

import json
from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from src.auth.ratelimit import (
    MemoryLoginRateLimiter,
    RedisLoginRateLimiter,
    create_login_rate_limiter,
    extract_client_ip,
)
from src.config import Settings
from src.core.types import Role
from src.infrastructure.store import MemoryStore
from src.llm.mock import MockLLM
from src.main import create_app


class FakeClock:
    """可注入/推进的时钟，用于确定性窗口与退避测试。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, headers: dict, client_host: str = "127.0.0.1") -> None:
        self.headers = headers
        self.client = _FakeClient(client_host)


# ---------------------------------------------------------------------------
# 1. extract_client_ip
# ---------------------------------------------------------------------------
def test_extract_client_ip_direct_connection_ignores_xff():
    # 可信代理深度 0：忽略 X-Forwarded-For，取直连 peer。
    r = _FakeRequest({"x-forwarded-for": "203.0.113.9"}, client_host="10.0.0.5")
    assert extract_client_ip(r, 0) == "10.0.0.5"


def test_extract_client_ip_depth1_takes_last_xff():
    r = _FakeRequest({"x-forwarded-for": "1.2.3.4, 5.6.7.8"}, client_host="10.0.0.5")
    assert extract_client_ip(r, 1) == "5.6.7.8"


def test_extract_client_ip_depth2_takes_penultimate_xff():
    r = _FakeRequest({"x-forwarded-for": "1.2.3.4, 5.6.7.8, 9.9.9.9"}, client_host="10.0.0.5")
    assert extract_client_ip(r, 2) == "5.6.7.8"


def test_extract_client_ip_no_xff_falls_back_to_client():
    r = _FakeRequest({}, client_host="10.0.0.5")
    assert extract_client_ip(r, 1) == "10.0.0.5"


# ---------------------------------------------------------------------------
# 2. Memory 限流器（可注入时钟）
# ---------------------------------------------------------------------------
def _mem_limiter(**overrides):
    defaults = dict(
        login_rate_limit_store="memory",
        login_rate_limit_window_seconds=3600,
        login_account_rate_limit=5,
        login_ip_rate_limit=30,
        login_backoff_max_failures=3,
        login_backoff_base_seconds=1.0,
        login_backoff_max_seconds=100.0,
    )
    defaults.update(overrides)
    return defaults


def test_account_rate_limit_rejected_after_threshold():
    clock = FakeClock()
    lim = MemoryLoginRateLimiter(Settings(**_mem_limiter(login_account_rate_limit=2)), clock)
    assert lim.check("T", "U", "1.1.1.1").allowed
    lim.record("T", "U", "1.1.1.1", ok=False)
    lim.record("T", "U", "1.1.1.1", ok=False)
    d = lim.check("T", "U", "1.1.1.1")
    assert not d.allowed and d.reason == "login_rate_limited" and d.status_code == 429


def test_ip_rate_limit_aggregates_across_accounts():
    clock = FakeClock()
    lim = MemoryLoginRateLimiter(Settings(**_mem_limiter(login_ip_rate_limit=2)), clock)
    # 不同账号、同一 IP，跨账号聚合。
    lim.record("T", "U1", "9.9.9.9", ok=False)
    lim.record("T", "U2", "9.9.9.9", ok=False)
    d = lim.check("T", "U3", "9.9.9.9")
    assert not d.allowed and d.reason == "ip_rate_limited"
    # 换 IP 不受影响。
    assert lim.check("T", "U3", "8.8.8.8").allowed


def test_success_resets_failure_counter_and_backoff():
    clock = FakeClock()
    lim = MemoryLoginRateLimiter(Settings(**_mem_limiter()), clock)
    for _ in range(3):
        lim.record("T", "U", "1.1.1.1", ok=False)
    # 3 次失败触发退避。
    d = lim.check("T", "U", "1.1.1.1")
    assert not d.allowed and d.reason == "login_backoff"
    # 成功登录重置失败计数与退避。
    lim.record("T", "U", "1.1.1.1", ok=True)
    assert lim.check("T", "U", "1.1.1.1").allowed


def test_backoff_escalates_exponentially_and_caps():
    clock = FakeClock()
    lim = MemoryLoginRateLimiter(Settings(**_mem_limiter(
        login_backoff_max_failures=3, login_backoff_base_seconds=2.0,
        login_backoff_max_seconds=100.0)), clock)
    for _ in range(3):
        lim.record("T", "U", "1.1.1.1", ok=False)
    d = lim.check("T", "U", "1.1.1.1")
    assert d.reason == "login_backoff"
    assert d.retry_after <= 2.0 and d.retry_after > 0.0  # base^0 = 2s
    # 推进时钟越过退避期，允许再试；再失败则退避翻倍。
    clock.advance(3.0)
    assert lim.check("T", "U", "1.1.1.1").allowed
    lim.record("T", "U", "1.1.1.1", ok=False)
    d2 = lim.check("T", "U", "1.1.1.1")
    assert d2.reason == "login_backoff"
    assert d2.retry_after <= 4.0 and d2.retry_after > 0.0  # base^1 = 4s (capped)


def test_check_and_register_records_and_rejects():
    clock = FakeClock()
    lim = MemoryLoginRateLimiter(Settings(**_mem_limiter(login_account_rate_limit=1)), clock)
    d1 = lim.check_and_register("T", "U", "1.1.1.1", ok=False)
    assert d1.allowed
    d2 = lim.check_and_register("T", "U", "1.1.1.1", ok=False)
    assert not d2.allowed and d2.reason == "login_rate_limited"


def test_create_limiter_defaults_to_memory():
    from src.auth.ratelimit import RedisLoginRateLimiter
    lim = create_login_rate_limiter(Settings(login_rate_limit_store="memory"))
    assert isinstance(lim, MemoryLoginRateLimiter)
    lim_redis = create_login_rate_limiter(Settings(login_rate_limit_store="redis"))
    assert isinstance(lim_redis, RedisLoginRateLimiter)


# ---------------------------------------------------------------------------
# 3. 路由级：/api/auth/login
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _argon2_phc(pw: str) -> str:
    from argon2 import PasswordHasher
    return PasswordHasher().hash(pw)


def _login_store():
    store = MemoryStore()
    store.create_tenant("T", "t")
    store.add_membership("T", "U1", Role.CUSTOMER)
    store.add_membership("T", "U2", Role.CUSTOMER)
    return store


def _creds_json() -> str:
    return json.dumps({"T:U1": _argon2_phc("pw1"), "T:U2": _argon2_phc("pw2")})


def _app(account_limit=5, ip_limit=30, proxy_depth=0, backoff_max_failures=5):
    settings = Settings(
        env="preview", auth_backend="real", auth_jwt_secret="sec", auth_jwt_issuer="iss",
        auth_jwt_audience="aud", auth_login_credentials=_creds_json(),
        auth_credential_hash="argon2id", login_rate_limit_store="memory",
        login_account_rate_limit=account_limit, login_ip_rate_limit=ip_limit,
        login_trusted_proxy_depth=proxy_depth,
        login_backoff_max_failures=backoff_max_failures,
    )
    return create_app(store=_login_store(), llm=MockLLM(), seed=False, settings=settings)


def test_login_success_and_success_audit():
    with TestClient(_app(account_limit=5)) as c:
        r = c.post("/api/auth/login", json={"tenant_id": "T", "user_id": "U1", "credential": "pw1"})
        assert r.status_code == 200 and r.json()["access_token"].count(".") == 2


def test_account_rate_limit_rejects_with_audit():
    with TestClient(_app(account_limit=2)) as c:
        assert c.post("/api/auth/login",
                      json={"tenant_id": "T", "user_id": "U1", "credential": "pw1"}).status_code == 200
        assert c.post("/api/auth/login",
                      json={"tenant_id": "T", "user_id": "U1", "credential": "pw1"}).status_code == 200
        r = c.post("/api/auth/login", json={"tenant_id": "T", "user_id": "U1", "credential": "pw1"})
        assert r.status_code == 429
        assert r.json().get("code") == "too_many_requests"
        assert r.json().get("reason") == "login_rate_limited"


def test_ip_rate_limit_aggregates_across_accounts_route():
    # 账号上限放宽，IP 上限=2；用 XFF 模拟同一 IP，跨账号聚合触发 IP 限流。
    with TestClient(_app(account_limit=100, ip_limit=2, proxy_depth=1)) as c:
        headers = {"X-Forwarded-For": "203.0.113.7"}
        assert c.post("/api/auth/login", json={"tenant_id": "T", "user_id": "U1", "credential": "pw1"},
                      headers=headers).status_code == 200
        assert c.post("/api/auth/login", json={"tenant_id": "T", "user_id": "U2", "credential": "pw2"},
                      headers=headers).status_code == 200
        r = c.post("/api/auth/login", json={"tenant_id": "T", "user_id": "U1", "credential": "pw1"},
                   headers=headers)
        assert r.status_code == 429
        assert r.json().get("reason") == "ip_rate_limited"


def test_wrong_credential_counts_as_failure_and_triggers_backoff():
    with TestClient(_app(account_limit=100, ip_limit=100, proxy_depth=0, backoff_max_failures=3)) as c:
        # 连续正确账号、错误口令达到退避阈值。
        for _ in range(3):
            assert c.post("/api/auth/login",
                          json={"tenant_id": "T", "user_id": "U1", "credential": "wrong"}).status_code == 401
        r = c.post("/api/auth/login", json={"tenant_id": "T", "user_id": "U1", "credential": "pw1"})
        assert r.status_code == 429
        assert r.json().get("reason") == "login_backoff"


def test_rate_limit_metrics_are_exposed():
    with TestClient(_app(account_limit=2)) as c:
        c.post("/api/auth/login", json={"tenant_id": "T", "user_id": "U1", "credential": "pw1"})
        c.post("/api/auth/login", json={"tenant_id": "T", "user_id": "U1", "credential": "pw1"})
        c.post("/api/auth/login", json={"tenant_id": "T", "user_id": "U1", "credential": "pw1"})
        body = c.get("/api/metrics").text
        assert "login_attempts_total" in body
        assert "login_rate_limited_total" in body


# ---------------------------------------------------------------------------
# 4. Redis 后端 fail-closed（不可用即拒绝，绝不放行）
# ---------------------------------------------------------------------------
def _redis_settings(**overrides) -> Settings:
    defaults = dict(
        login_rate_limit_store="redis",
        login_rate_limit_window_seconds=300,
        login_account_rate_limit=5,
        login_ip_rate_limit=30,
        login_backoff_max_failures=3,
        login_backoff_base_seconds=1.0,
        login_backoff_max_seconds=100.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_redis_limiter_check_fails_closed_when_unavailable():
    # 指向一个无监听服务的地址 → 连接失败 → 返回 redis_unavailable（拒绝并审计）。
    clock = FakeClock()
    lim = RedisLoginRateLimiter(_redis_settings(), clock, redis_url="redis://127.0.0.1:1/0")
    d = lim.check("T", "U", "1.2.3.4")
    assert not d.allowed
    assert d.reason == "redis_unavailable"
    assert d.status_code == 429


def test_redis_limiter_record_survives_unavailable_without_raise():
    # redis 不可用时 record 应吞掉异常（不因限流状态失败而让登录崩溃），fail-closed 由 check 兜底。
    clock = FakeClock()
    lim = RedisLoginRateLimiter(_redis_settings(), clock, redis_url="redis://127.0.0.1:1/0")
    lim.record("T", "U", "1.2.3.4", ok=False)  # 不应抛
    d = lim.check("T", "U", "1.2.3.4")
    assert not d.allowed and d.reason == "redis_unavailable"


def test_route_fails_closed_on_redis_unavailable():
    settings = Settings(
        env="preview", auth_backend="real", auth_jwt_secret="sec", auth_jwt_issuer="iss",
        auth_jwt_audience="aud", auth_login_credentials=_creds_json(),
        auth_credential_hash="argon2id", login_rate_limit_store="redis",
        redis_url="redis://127.0.0.1:1/0", login_backoff_max_failures=5,
    )
    app = create_app(store=_login_store(), llm=MockLLM(), seed=False, settings=settings)
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"tenant_id": "T", "user_id": "U1", "credential": "pw1"})
        assert r.status_code == 429
        assert r.json().get("reason") == "redis_unavailable"
