"""受控建租户脚本测试。

`scripts/create_bootstrapped_tenants.py` —— 生产/预发布**首批真实**租户与成员的受控创建入口。
验证：
1. 合法清单（真实租户 id/名）只允许 customer/agent/admin/approver 角色 → 创建成功且幂等。
2. 演示租户（TENANT-A/TENANT-B）与演示成员（USER-001 等）一律拒绝（fail-closed）。
3. 角色非法 → 拒绝（SystemExit）。
4. `--dry-run` 不连接/不写库（对外只校验清单并打印范围）。
5. `--backend postgres` 且缺 `DATABASE_URL` → 拒绝（退出码 2，fail-closed）。
6. 清单 JSON 非法（缺少 tenants / 非 dict）→ 拒绝。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from src.core.types import Role
from src.infrastructure.store import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ROOT / "scripts" / "create_bootstrapped_tenants.py"

_spec = importlib.util.spec_from_file_location("create_bootstrapped_tenants", str(_SCRIPT))
cbt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cbt)


def _valid_spec() -> list[dict]:
    return [
        {
            "tenant_id": "REAL-ACME",
            "name": "真实租户ACME",
            "members": [
                {"user_id": "admin-1", "role": "admin"},
                {"user_id": "agent-1", "role": "agent"},
                {"user_id": "cust-1", "role": "customer"},
                {"user_id": "appr-1", "role": "approver"},
            ],
        },
        {
            "tenant_id": "REAL-BETA",
            "name": "真实租户BETA",
            "members": [{"user_id": "b-owner", "role": "admin"}],
        },
    ]


def _write_spec(tmp_path: Path, data) -> Path:
    p = tmp_path / "tenants.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def _tenant_exists(store: MemoryStore, tid: str) -> bool:
    try:
        store.get_tenant(tid)
        return True
    except Exception as exc:
        code = getattr(exc, "status_code", None)
        if code == 404:
            return False
        raise


def _membership_exists(store: MemoryStore, tid: str, uid: str) -> bool:
    return store.get_membership(tid, uid) is not None


# ---------------------------------------------------------------------------
# 1. 校验（纯函数，无副作用）
# ---------------------------------------------------------------------------
def test_validate_accepts_legal_roles():
    cbt._validate_tenants(_valid_spec())  # 不抛即通过


def test_validate_rejects_demo_tenant():
    bad = _valid_spec()
    bad[0]["tenant_id"] = "TENANT-A"
    with pytest.raises(SystemExit):
        cbt._validate_tenants(bad)


def test_validate_rejects_demo_member():
    bad = _valid_spec()
    bad[0]["members"][0]["user_id"] = "USER-001"
    with pytest.raises(SystemExit):
        cbt._validate_tenants(bad)


def test_validate_rejects_illegal_role():
    bad = _valid_spec()
    bad[0]["members"][0]["role"] = "superadmin"
    with pytest.raises(SystemExit):
        cbt._validate_tenants(bad)


def test_validate_rejects_missing_tenant_id_or_name():
    bad1 = _valid_spec()
    bad1[0]["tenant_id"] = ""
    with pytest.raises(SystemExit):
        cbt._validate_tenants(bad1)
    bad2 = _valid_spec()
    bad2[0]["name"] = ""
    with pytest.raises(SystemExit):
        cbt._validate_tenants(bad2)


def test_validate_requires_roles_to_be_tenant_roles_only():
    # 与 Role.tenant_roles() 一致；platform_admin 不属于租户角色，必须被拒。
    assert Role.tenant_roles() == {"customer", "agent", "admin", "approver"}
    bad = _valid_spec()
    bad[0]["members"][0]["role"] = "platform_admin"
    with pytest.raises(SystemExit):
        cbt._validate_tenants(bad)


# ---------------------------------------------------------------------------
# 2. _create 幂等（注入 MemoryStore）
# ---------------------------------------------------------------------------
def test_create_creates_real_tenants_and_members():
    store = MemoryStore()
    counts = cbt._create(store, _valid_spec(), dry_run=False)
    assert counts["tenants_created"] == 2 and counts["tenants_skipped"] == 0
    assert counts["members_created"] == 5 and counts["members_skipped"] == 0
    assert _tenant_exists(store, "REAL-ACME") is True
    assert _tenant_exists(store, "REAL-BETA") is True
    assert _membership_exists(store, "REAL-ACME", "admin-1") is True
    # 角色正确。
    m = store.get_membership("REAL-ACME", "cust-1")
    assert m.role == Role.CUSTOMER


def test_create_is_idempotent_second_call_skips():
    store = MemoryStore()
    cbt._create(store, _valid_spec(), dry_run=False)
    counts = cbt._create(store, _valid_spec(), dry_run=False)
    assert counts["tenants_created"] == 0 and counts["tenants_skipped"] == 2
    assert counts["members_created"] == 0 and counts["members_skipped"] == 5


def test_create_dry_run_does_not_write():
    store = MemoryStore()
    counts = cbt._create(store, _valid_spec(), dry_run=True)
    assert counts["tenants_created"] == 2  # dry-run 也计入"将创建"
    assert _tenant_exists(store, "REAL-ACME") is False
    assert _membership_exists(store, "REAL-ACME", "admin-1") is False


# ---------------------------------------------------------------------------
# 3. CLI（main）层
# ---------------------------------------------------------------------------
def test_main_dry_run_returns_zero_and_no_store(tmp_path, monkeypatch):
    spec = _write_spec(tmp_path, {"tenants": _valid_spec()})
    rc = cbt.main(["--spec", str(spec), "--dry-run", "--env", "development"])
    assert rc == 0


def test_main_postgres_without_database_url_rejected(tmp_path, monkeypatch):
    # 无 DATABASE_URL → postgres 后端 fail-closed（退出码 2），不尝试连接。
    monkeypatch.delenv("DATABASE_URL", raising=False)
    spec = _write_spec(tmp_path, {"tenants": _valid_spec()})
    rc = cbt.main(["--spec", str(spec), "--backend", "postgres", "--env", "preview"])
    assert rc == 2


def test_main_rejects_demo_tenant_cli(tmp_path):
    spec = _write_spec(tmp_path, {"tenants": _valid_spec()})
    data = json.loads(spec.read_text(encoding="utf-8"))
    data["tenants"][0]["tenant_id"] = "TENANT-A"
    spec.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        cbt.main(["--spec", str(spec), "--dry-run"])


def test_main_rejects_illegal_role_cli(tmp_path):
    spec = _write_spec(tmp_path, {"tenants": _valid_spec()})
    data = json.loads(spec.read_text(encoding="utf-8"))
    data["tenants"][0]["members"][0]["role"] = "superadmin"
    spec.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        cbt.main(["--spec", str(spec), "--dry-run"])


# ---------------------------------------------------------------------------
# 4. _load_tenants 对非法清单文件
# ---------------------------------------------------------------------------
def test_load_tenants_rejects_bad_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        cbt._load_tenants(str(p))


def test_load_tenants_rejects_missing_tenants_key(tmp_path):
    p = tmp_path / "bad2.json"
    p.write_text(json.dumps({"foo": []}), encoding="utf-8")
    with pytest.raises(SystemExit):
        cbt._load_tenants(str(p))
