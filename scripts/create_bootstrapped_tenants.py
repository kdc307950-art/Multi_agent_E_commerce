"""create_bootstrapped_tenants.py — 受控创建首批**真实**租户与成员。

**用途与红线**：
- 生产/预发布首批真实租户与成员**必须**通过本脚本或受控迁移创建，**禁止**依赖
  `seed_default`（`seed_default` 仅用于 development/test 的演示数据，见 `src/main.py` 的
  `demo_seed_gate` 门控：受限环境（preview/production）一律拒绝演示 seed）。
- 本脚本**绝不**生成/包含演示租户 `TENANT-A` / `TENANT-B` 或任何演示成员；若清单含演示
  租户 id 或演示成员 id 即拒绝（fail-closed），防止误把演示数据导入生产数据面。
- 不打印任何凭据：本脚本只接收角色（customer/agent/admin/approver），不涉及口令/令牌。

**幂等**：已存在的租户/成员一律跳过，不重复创建、不覆盖既有角色（仅在租户/成员不存在时创建）。

**用法**：
    python scripts/create_bootstrapped_tenants.py --spec ./tenants.json [--dry-run]
        [--backend postgres|memory|sqlite] [--env preview]

**清单 JSON 格式**（`--spec`）：
    {
      "tenants": [
        {
          "tenant_id": "REAL-ACME",
          "name": "真实租户ACME",
          "members": [
            {"user_id": "admin-1", "role": "admin"},
            {"user_id": "agent-1",  "role": "agent"},
            {"user_id": "cust-1",   "role": "customer"}
          ]
        }
      ]
    }

角色限 `customer` / `agent` / `admin` / `approver`（`Role.tenant_roles()`）。

`--dry-run`：只校验清单并打印"将创建"的范围，**不连接**任何存储、**不写库**，可用于
安全预览（实际跳过数取决于现有数据）。

`--backend`：默认 `postgres`（生产；连接串取 `DATABASE_URL` / `database_url`）。
`memory` / `sqlite` 仅用于本地/测试模拟。生产必须有 PostgreSQL 且 `DATABASE_URL` 非空。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# 将仓库根加入 sys.path，允许 `python scripts/create_bootstrapped_tenants.py` 直接运行。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Settings
from src.core.types import DomainError, Role
from src.infrastructure.store import build_store

# 演示租户/成员：与 src/main.py::seed_default 保持一致，本脚本一律拒绝。
DEMO_TENANT_IDS = frozenset({"TENANT-A", "TENANT-B"})
DEMO_MEMBER_IDS = frozenset({
    "USER-001", "USER-002", "AGENT-A", "ADMIN-A", "APPROVER-A",
    "USER-B1", "USER-B2", "AGENT-B", "ADMIN-B", "APPROVER-B",
})


def _build_settings(args: argparse.Namespace) -> Settings:
    """构造 Settings：优先读环境变量 / .env，再按 CLI 覆盖 env / storage_backend。

    `database_url`（来自 `DATABASE_URL` 环境变量或 `.env`）用于 PostgresStore 连接。
    """
    kwargs: dict = {}
    if args.env:
        kwargs["env"] = args.env
    if args.backend:
        kwargs["storage_backend"] = args.backend
    return Settings(**kwargs)


def _load_tenants(spec_path: str) -> list[dict]:
    """读取并基础校验 JSON 清单，返回 `tenants` 列表。"""
    with open(spec_path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict) or "tenants" not in data:
        raise SystemExit('清单必须为 {"tenants": [...]}')
    tenants = data["tenants"]
    if not isinstance(tenants, list):
        raise SystemExit("tenants 必须为列表")
    return tenants


def _validate_tenants(tenants: list[dict]) -> None:
    """校验清单合法性与安全红线；违规即抛 SystemExit（fail-closed）。"""
    valid_roles = Role.tenant_roles()
    for t in tenants:
        tid = str(t.get("tenant_id") or "").strip()
        if not tid:
            raise SystemExit("每个租户必须提供非空 tenant_id")
        if tid in DEMO_TENANT_IDS:
            raise SystemExit(
                f"清单含演示租户 {tid}；本脚本禁止创建演示数据。演示数据仅限 development/test "
                "通过 seed_default 创建（见 src/main.py demo_seed_gate）。")
        if not str(t.get("name") or "").strip():
            raise SystemExit(f"租户 {tid} 缺少 name")
        members = t.get("members", [])
        if not isinstance(members, list):
            raise SystemExit(f"租户 {tid} 的 members 必须为列表")
        for m in members:
            uid = str(m.get("user_id") or "").strip()
            role = str(m.get("role") or "").strip()
            if not uid:
                raise SystemExit(f"租户 {tid} 某成员缺少 user_id")
            if uid in DEMO_MEMBER_IDS:
                raise SystemExit(
                    f"清单含演示成员 {uid}（租户 {tid}）；本脚本禁止创建演示成员。")
            if role not in valid_roles:
                raise SystemExit(
                    f"租户 {tid} 成员 {uid} 角色 {role!r} 非法；"
                    f"仅允许 {sorted(valid_roles)}")


def _tenant_exists(store, tenant_id: str) -> bool:
    try:
        store.get_tenant(tenant_id)
        return True
    except DomainError as exc:
        if exc.status_code == 404:
            return False
        raise


def _membership_exists(store, tenant_id: str, user_id: str) -> bool:
    return store.get_membership(tenant_id, user_id) is not None


def _create(store, tenants: list[dict], dry_run: bool) -> dict:
    """按幂等语义创建租户与成员；返回统计（created/skipped），不打印任何凭据。"""
    counts = {"tenants_created": 0, "tenants_skipped": 0,
              "members_created": 0, "members_skipped": 0}
    for t in tenants:
        tid = str(t["tenant_id"]).strip()
        name = str(t["name"]).strip()
        exists = _tenant_exists(store, tid) if not dry_run else False
        if exists:
            counts["tenants_skipped"] += 1
            print(f"  [skip]   租户 {tid} 已存在")
        else:
            if not dry_run:
                store.create_tenant(tid, name)
            counts["tenants_created"] += 1
            print(f"  [{ 'dry-run' if dry_run else 'create'}] 租户 {tid} ({name})")

        for m in t.get("members", []):
            uid = str(m["user_id"]).strip()
            role = Role(str(m["role"]).strip())
            if dry_run:
                counts["members_created"] += 1
                print(f"  [dry-run] 成员 {tid}:{uid} -> {role.value}")
                continue
            if _membership_exists(store, tid, uid):
                counts["members_skipped"] += 1
                print(f"  [skip]   成员 {tid}:{uid} 已存在")
                continue
            store.add_membership(tid, uid, role)
            counts["members_created"] += 1
            print(f"  [create] 成员 {tid}:{uid} -> {role.value}")
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="受控创建首批真实租户与成员（幂等）")
    parser.add_argument("--spec", required=True, help="租户/成员清单 JSON 路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验并打印将创建的范围，不连接/不写库")
    parser.add_argument("--backend", default="postgres",
                        choices=["postgres", "memory", "sqlite"],
                        help="存储后端（生产使用 postgres；memory/sqlite 仅本地模拟）")
    parser.add_argument("--env", default="preview",
                        choices=["development", "test", "preview", "production"])
    args = parser.parse_args(argv)

    tenants = _load_tenants(args.spec)
    _validate_tenants(tenants)

    if args.dry_run:
        tenant_count = len(tenants)
        member_count = sum(len(t.get("members", [])) for t in tenants)
        print(f"[dry-run] 清单校验通过：将创建 {tenant_count} 个租户、{member_count} 个成员"
              f"（不连接存储、不写库；实际跳过数取决于现有数据）。")
        for t in tenants:
            print(f"  - 租户 {t['tenant_id']}（{t['name']}），成员 "
                  f"{[m['user_id'] for m in t.get('members', [])]}")
        return 0

    settings = _build_settings(args)
    if settings.storage_backend == "postgres" and not settings.database_url:
        print("错误：--backend postgres 需要非空 DATABASE_URL / database_url；"
              "当前为空，拒绝执行（fail-closed）。", file=sys.stderr)
        return 2
    store = build_store(settings)
    try:
        counts = _create(store, tenants, dry_run=False)
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    print(f"\n[SUMMARY] 租户 创建 {counts['tenants_created']} / 跳过 {counts['tenants_skipped']}；"
          f"成员 创建 {counts['members_created']} / 跳过 {counts['members_skipped']}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
