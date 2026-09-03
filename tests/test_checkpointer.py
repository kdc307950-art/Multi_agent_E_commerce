"""TenantScopedCheckpointer 的可信作用域校验逻辑测试（无 PG；不触碰真实连接）。"""
from __future__ import annotations

import pytest

from src.core.types import DomainError
from src.infrastructure.checkpointer import (
    CheckpointRequestScope,
    TenantScopedCheckpointer,
    require_checkpoint_scope,
    reset_checkpoint_scope,
    set_checkpoint_scope,
)


def _bare_checkpointer() -> TenantScopedCheckpointer:
    # 绕过 __init__（需要事件循环 + 真实连接），只测作用域校验逻辑。
    return TenantScopedCheckpointer.__new__(TenantScopedCheckpointer)


def test_require_scope_missing_rejected():
    # 未建立可信作用域时默认拒绝（fail-closed）。
    reset_checkpoint_scope(None)
    with pytest.raises(DomainError) as ei:
        require_checkpoint_scope()
    assert ei.value.code == "missing_tenant_context"


def test_require_scope_roundtrip():
    scope = CheckpointRequestScope(tenant_id="T", user_id="U", thread_id="th-1", source="request")
    token = set_checkpoint_scope(scope)
    assert require_checkpoint_scope() == scope
    reset_checkpoint_scope(token)
    with pytest.raises(DomainError):
        require_checkpoint_scope()


def test_scope_for_thread_mismatch_rejected():
    cp = _bare_checkpointer()
    scope = CheckpointRequestScope(tenant_id="T", user_id="U", thread_id="th-1", source="request")
    token = set_checkpoint_scope(scope)
    try:
        # 请求的 thread 与可信作用域不一致 → 拒绝（跨租户线程恢复防护）。
        with pytest.raises(DomainError) as ei:
            cp._scope_for_thread("th-other")
        assert ei.value.code == "cross_tenant_denied"
    finally:
        reset_checkpoint_scope(token)


def test_scope_for_thread_match_ok():
    cp = _bare_checkpointer()
    scope = CheckpointRequestScope(tenant_id="T", user_id="U", thread_id="th-1", source="history")
    token = set_checkpoint_scope(scope)
    try:
        assert cp._scope_for_thread("th-1").thread_id == "th-1"
    finally:
        reset_checkpoint_scope(token)


def test_setup_forbidden():
    # 官方表由迁移阶段初始化；运行时调用 setup() 必须被拒绝（防绕 RLS）。
    cp = _bare_checkpointer()
    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.run(cp.setup())
