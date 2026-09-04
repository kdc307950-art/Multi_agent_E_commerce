"""seed 门控测试。

验证安全基线（与 AGENTS.md 对齐）：
- 演示租户（TENANT-A / TENANT-B）只能用于本地开发/测试，**绝不**进入 preview/production。
- `demo_seed_gate`：受限环境（preview/production）无论 `DEMO_SEED_ENABLED` 与 `seed_requested`
  取何值，一律**不创建**任何演示租户；若还显式设置 `DEMO_SEED_ENABLED=true` 属非法配置，
  **启动即 fail-closed（抛 RuntimeError）**。
- `create_app(seed=True, settings=...)` 级验证：仅 development/test 且 `DEMO_SEED_ENABLED=true`
  才 seed 出 TENANT-A/TENANT-B；受限环境即使 seed=True 也不出现。
"""
from __future__ import annotations

import pytest

from src.config import Settings
from src.core.types import DomainError, Role
from src.infrastructure.store import MemoryStore
from src.llm.mock import MockLLM
from src.main import create_app, demo_seed_gate


def _tenant_exists(store: MemoryStore, tenant_id: str) -> bool:
    try:
        store.get_tenant(tenant_id)
        return True
    except DomainError as exc:
        if exc.status_code == 404:
            return False
        raise


def _membership_exists(store: MemoryStore, tenant_id: str, user_id: str) -> bool:
    return store.get_membership(tenant_id, user_id) is not None


# ---------------------------------------------------------------------------
# 1. demo_seed_gate 纯函数（环境 × DEMO_SEED_ENABLED × seed_requested）
# ---------------------------------------------------------------------------
def test_gate_restricted_env_disabled_returns_false_even_if_seed_requested():
    # preview + 未显式启用 seed + 调用方传 seed=True → False（安全默认，不创建演示租户）。
    assert demo_seed_gate(Settings(env="preview", demo_seed_enabled=False), True) is False


def test_gate_restricted_env_always_skips_regardless_of_request():
    # production + 未启用 seed + 调用方传 seed=True/False → 均 False。
    assert demo_seed_gate(Settings(env="production", demo_seed_enabled=False), True) is False
    assert demo_seed_gate(Settings(env="production", demo_seed_enabled=False), False) is False


def test_gate_restricted_env_with_demo_enabled_raises_fail_closed():
    # 受限环境 + DEMO_SEED_ENABLED=true → 非法配置，启动 fail-closed。
    with pytest.raises(RuntimeError):
        demo_seed_gate(Settings(env="preview", demo_seed_enabled=True), True)
    with pytest.raises(RuntimeError):
        demo_seed_gate(Settings(env="production", demo_seed_enabled=True), True)


def test_gate_development_enabled_and_requested_allows():
    assert demo_seed_gate(Settings(env="development", demo_seed_enabled=True), True) is True


def test_gate_test_enabled_and_requested_allows():
    assert demo_seed_gate(Settings(env="test", demo_seed_enabled=True), True) is True


def test_gate_development_without_enabled_returns_false():
    # development + 未显式启用 DEMO_SEED_ENABLED → 即使 seed_requested=True 也禁止。
    assert demo_seed_gate(Settings(env="development", demo_seed_enabled=False), True) is False


def test_gate_development_without_request_returns_false():
    # development + 启用 seed 但调用方未请求（seed_requested=False）→ False。
    assert demo_seed_gate(Settings(env="development", demo_seed_enabled=True), False) is False


def test_gate_restricted_env_default_settings_never_allows():
    # 默认 Settings（env=development, demo_seed_enabled=False）不启用 seed。
    assert demo_seed_gate(Settings(), True) is False


# ---------------------------------------------------------------------------
# 2. create_app(seed=...) 级：受限环境永远不 seed 演示租户
# ---------------------------------------------------------------------------
def _preview_app(seed: bool, demo_seed_enabled: bool):
    store = MemoryStore()
    app = create_app(
        store=store, llm=MockLLM(), seed=seed,
        settings=Settings(env="preview", auth_backend="real", auth_jwt_secret="verify-secret",
                          auth_jwt_issuer="iss", auth_jwt_audience="aud",
                          demo_seed_enabled=demo_seed_enabled),
    )
    return store, app


def test_create_app_preview_seed_true_and_disabled_never_creates_demo_tenant():
    # 受限环境 + seed=True + DEMO_SEED_ENABLED=false → 不创建 TENANT-A/B，且不抛（跳过 seed）。
    store, _app = _preview_app(seed=True, demo_seed_enabled=False)
    assert _tenant_exists(store, "TENANT-A") is False
    assert _tenant_exists(store, "TENANT-B") is False
    assert _membership_exists(store, "TENANT-A", "USER-001") is False


def test_create_app_preview_seed_false_and_disabled_never_creates():
    store, _app = _preview_app(seed=False, demo_seed_enabled=False)
    assert _tenant_exists(store, "TENANT-A") is False


def test_create_app_preview_demo_enabled_raises_fail_closed():
    # 受限环境 + DEMO_SEED_ENABLED=true（即使 seed=True 且配置了 real 认证）→ 启动 fail-closed。
    with pytest.raises(RuntimeError):
        _preview_app(seed=True, demo_seed_enabled=True)


# ---------------------------------------------------------------------------
# 3. create_app(seed=...) 级：非受限环境仅在显式启用时 seed
# ---------------------------------------------------------------------------
def _dev_app(seed: bool, demo_seed_enabled: bool):
    store = MemoryStore()
    app = create_app(
        store=store, llm=MockLLM(), seed=seed,
        settings=Settings(env="development", demo_seed_enabled=demo_seed_enabled),
    )
    return store, app


def test_create_app_development_enabled_seeds_demo_tenants():
    store, app = _dev_app(seed=True, demo_seed_enabled=True)
    assert _tenant_exists(store, "TENANT-A") is True
    assert _tenant_exists(store, "TENANT-B") is True
    assert _membership_exists(store, "TENANT-A", "USER-001") is True
    assert _membership_exists(store, "TENANT-A", "APPROVER-A") is True
    assert _membership_exists(store, "TENANT-B", "ADMIN-B") is True


def test_create_app_development_not_enabled_skips_seed():
    store, app = _dev_app(seed=True, demo_seed_enabled=False)
    assert _tenant_exists(store, "TENANT-A") is False


def test_create_app_development_seed_false_skips_even_if_enabled():
    # 调用方未请求 seed（seed=False）+ 显式启用 → 仍不 seed（双保险门控）。
    store, app = _dev_app(seed=False, demo_seed_enabled=True)
    assert _tenant_exists(store, "TENANT-A") is False


def test_create_app_test_env_enabled_seeds_demo_tenants():
    store = MemoryStore()
    create_app(
        store=store, llm=MockLLM(), seed=True,
        settings=Settings(env="test", demo_seed_enabled=True),
    )
    assert _tenant_exists(store, "TENANT-A") is True
