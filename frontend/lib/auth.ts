"use strict";
// 前端认证/角色（Mock）。身份与授权边界由后端 TenantContext 强制；前端只做展示与路由分发。
// Mock 令牌结构：mock:<tenant>:<user>:<role>:<entropy>（与后端 issue_token 一致）。
// 生产环境由后端签发真实令牌，本文件仅用于本地开发演示。
import type { AuthUser, Role } from "./types";

const TOKEN_PREFIX = "mock";

export const ROLE_LABEL: Record<Role, string> = {
  customer: "客户",
  agent: "客服",
  admin: "管理员",
  approver: "审批人",
};

export interface MenuItem {
  key: string;
  label: string;
  href: string;
}

// 各角色可见菜单（前端按角色渲染；后端仍强制授权）。
export const ROLE_MENUS: Record<Role, MenuItem[]> = {
  customer: [
    { key: "chat", label: "客户会话", href: "/chat" },
    { key: "sessions", label: "我的会话", href: "/sessions" },
  ],
  agent: [
    { key: "workbench", label: "客服工作台", href: "/workbench" },
    { key: "sessions", label: "会话历史", href: "/sessions" },
    { key: "approvals", label: "审批中心（只读）", href: "/approvals" },
  ],
  admin: [
    { key: "workbench", label: "客服工作台", href: "/workbench" },
    { key: "sessions", label: "会话历史", href: "/sessions" },
    { key: "approvals", label: "审批中心", href: "/approvals" },
    { key: "settings", label: "成员与设置", href: "/settings" },
  ],
  approver: [
    { key: "workbench", label: "客服工作台", href: "/workbench" },
    { key: "approvals", label: "审批中心", href: "/approvals" },
  ],
};

export const ROLE_HOME: Record<Role, string> = {
  customer: "/chat",
  agent: "/workbench",
  admin: "/workbench",
  approver: "/approvals",
};

// 首次默认落地的租户/成员（与后端 seed 一致），用于登录选择。
export interface IdentityOption {
  label: string;
  tenantId: string;
  tenantName: string;
  userId: string;
  role: Role;
}

export const IDENTITY_OPTIONS: IdentityOption[] = [
  { label: "客户（租户A / USER-001）", tenantId: "TENANT-A", tenantName: "租户A", userId: "USER-001", role: "customer" },
  { label: "客服（租户A / AGENT-A）", tenantId: "TENANT-A", tenantName: "租户A", userId: "AGENT-A", role: "agent" },
  { label: "审批人（租户A / APPROVER-A）", tenantId: "TENANT-A", tenantName: "租户A", userId: "APPROVER-A", role: "approver" },
  { label: "管理员（租户A / ADMIN-A）", tenantId: "TENANT-A", tenantName: "租户A", userId: "ADMIN-A", role: "admin" },
  { label: "客户（租户B / USER-B1）", tenantId: "TENANT-B", tenantName: "租户B", userId: "USER-B1", role: "customer" },
  { label: "管理员（租户B / ADMIN-B）", tenantId: "TENANT-B", tenantName: "租户B", userId: "ADMIN-B", role: "admin" },
];

export function makeToken(tenantId: string, userId: string, role: Role): string {
  const entropy = Array.from({ length: 16 }, () =>
    Math.floor(Math.random() * 16).toString(16),
  ).join("");
  return `${TOKEN_PREFIX}:${tenantId}:${userId}:${role}:${entropy}`;
}

const AUTH_KEY = "dsh_after_sales_auth_v1";

export function saveAuth(user: AuthUser): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(AUTH_KEY, JSON.stringify(user));
}

export function loadAuth(): AuthUser | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(AUTH_KEY);
    if (!raw) return null;
    const u = JSON.parse(raw) as AuthUser;
    if (!u.token || !u.role || !u.tenantId) return null;
    return u;
  } catch {
    return null;
  }
}

export function clearAuth(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem(AUTH_KEY);
}
