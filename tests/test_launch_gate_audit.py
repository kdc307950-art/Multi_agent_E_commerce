"""上线门控 + 受控审计端点 + 指标端点验收测试。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.auth.security import issue_token
from src.config import Settings
from src.core.launch_gate import (
    enforce_strict,
    launch_allowlist,
    tenant_allowed_for_launch,
    verify_launch_gate,
)
from src.core.types import Role
from src.main import create_app
from tests.conftest import bearer
from src.llm.mock import MockLLM
from src.infrastructure.store import MemoryStore


def _new_session(client: TestClient, token: str) -> str:
    r = client.post("/api/sessions", headers=bearer(token))
    assert r.status_code == 201, r.text
    return r.json()["thread_id"]


# ---- 门控纯函数 ----
def test_launch_allowlist_and_tenant_allowed():
    s = Settings(_env_file=None, env="preview", storage_backend="postgres",
                 launch_allowed_tenants="TENANT-A, TENANT-C", auth_backend="real",
                 auth_jwt_secret="x")
    assert launch_allowlist(s) == {"TENANT-A", "TENANT-C"}
    assert tenant_allowed_for_launch(s, "TENANT-A") is True
    assert tenant_allowed_for_launch(s, "TENANT-B") is False
    # 空白名单 → 全放开
    s2 = Settings(_env_file=None, env="preview", storage_backend="postgres",
                  launch_allowed_tenants="", auth_backend="real", auth_jwt_secret="x")
    assert tenant_allowed_for_launch(s2, "ANY") is True


def test_verify_launch_gate_flags_restricted_without_allowlist():
    s = Settings(_env_file=None, env="preview", storage_backend="postgres",
                 launch_allowed_tenants="", auth_backend="real", auth_jwt_secret="x",
                 execution_mode="shadow", execution_provider="mock")
    report = verify_launch_gate(s)
    assert report["ok"] is False
    assert any("LAUNCH_ALLOWED_TENANTS" in v for v in report["violations"])


def test_verify_launch_gate_flags_live_with_mock_provider():
    s = Settings(_env_file=None, env="preview", storage_backend="postgres",
                 launch_allowed_tenants="TENANT-A", auth_backend="real", auth_jwt_secret="x",
                 execution_mode="live", execution_provider="mock")
    report = verify_launch_gate(s)
    assert report["ok"] is False
    assert any("EXECUTION_MODE=live" in v for v in report["violations"])


def test_launch_gate_ok_when_satisfied_and_strict_passes():
    s = Settings(_env_file=None, env="preview", storage_backend="postgres",
                 launch_allowed_tenants="TENANT-A", auth_backend="real", auth_jwt_secret="x",
                 execution_mode="shadow", execution_provider="mock",
                 launch_require_approval=True, launch_full_audit=True,
                 launch_manual_review=True, launch_gate_strict=True)
    assert verify_launch_gate(s)["ok"] is True
    enforce_strict(s)  # 不抛


def test_enforce_strict_raises_on_violation():
    s = Settings(_env_file=None, env="preview", storage_backend="memory",
                 launch_allowed_tenants="", launch_gate_strict=True,
                 auth_backend="real", auth_jwt_secret="x")
    with pytest.raises(RuntimeError):
        enforce_strict(s)


# ---- 上线白名单在 API 层强制 ----
def _make_app_with_allowlist(store, llm, allowlist: str):
    settings = Settings(_env_file=None, env="development", storage_backend="memory",
                        launch_allowed_tenants=allowlist)
    return create_app(store=store, llm=llm, seed=False, settings=settings)


def test_launch_allowlist_denies_non_allowlisted_tenant():
    store = MemoryStore()
    store.create_tenant("TENANT-A", "租户A")
    store.create_tenant("TENANT-B", "租户B")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)
    app = _make_app_with_allowlist(store, MockLLM(), "TENANT-A")
    with TestClient(app) as client:
        ok = client.post("/api/sessions", headers=bearer(issue_token("TENANT-A", "USER-001", Role.CUSTOMER)))
        assert ok.status_code == 201
        denied = client.post("/api/sessions", headers=bearer(issue_token("TENANT-B", "USER-B1", Role.CUSTOMER)))
        assert denied.status_code == 403
        # 拒绝已写审计
        actions = [a.action for a in store.list_audit("TENANT-B")]
        assert any("launch_tenant_not_allowed" in a for a in actions)


# ---- 受控审计端点 ----
def test_audit_endpoint_admin_sees_tenant_wide_and_redacted(client, store):
    tok_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    _new_session(client, tok_admin)
    store.append_audit("TENANT-A", "ADMIN-A", "test.sensitive", "approval", "appr-1",
                       {"address": "北京市朝阳区建国路88号", "card": "6222000011112222"},
                       time.time())
    r = client.get("/api/audit", headers=bearer(tok_admin))
    assert r.status_code == 200
    recs = r.json()
    assert any(x["action"] == "session.create" for x in recs)
    sensitive = next(x for x in recs if x["action"] == "test.sensitive")
    assert "建国路" not in str(sensitive["detail"])
    assert "6222000011112222" not in str(sensitive["detail"])


def test_audit_endpoint_customer_only_own_threads(client, store):
    tok_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    user_thread = _new_session(client, tok_user)
    tok_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    _new_session(client, tok_admin)
    r = client.get("/api/audit", headers=bearer(tok_user))
    assert r.status_code == 200
    recs = r.json()
    # customer 只看到本人会话相关（不泄露 admin 会话）
    for x in recs:
        assert x["target_type"] == "session" and x["target_id"] == user_thread


def test_audit_endpoint_denial_cross_tenant_404(client, store):
    tok_b = issue_token("TENANT-B", "ADMIN-B", Role.ADMIN)
    r = client.get("/api/audit", headers=bearer(tok_b))
    assert r.status_code == 200
    # 只返回 TENANT-B 的审计（空），绝不串租户
    assert r.json() == []


# ---- 指标端点 ----
def test_metrics_endpoint_no_tenant_label(client):
    r = client.get("/api/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "tenant_id" not in r.text
    assert any(line.startswith("# TYPE") for line in r.text.splitlines())
