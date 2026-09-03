"use strict";
// 前端契约类型（与后端 OpenAPI / 《生产环境架构设计》§3 保持一致）。
// 前端不得提交 tenant_id / user_id；身份与归属由服务端 TenantContext 决定。

export type SseEventType =
  | "accepted"
  | "token"
  | "node"
  | "approval_required"
  | "done"
  | "error";

export interface ChatStartBody {
  mode: "start";
  thread_id: string;
  client_request_id: string;
  message: string;
}

export interface ChatResumeBody {
  mode: "resume";
  stream_id: string;
}

export interface SseFrame {
  id: number;
  event: SseEventType;
  data: Record<string, unknown>;
}

export interface SessionRead {
  thread_id: string;
  title: string | null;
  created_at: number;
  expires_at: number;
  status: string;
  message_count: number;
  user_id?: string | null;
}

export interface MemberRead {
  tenant_id: string;
  user_id: string;
  role: string;
  status: string;
}

export interface OrderItemRead {
  name: string;
  sku?: string | null;
  price?: number | null;
  quantity?: number;
}

export interface OrderRead {
  order_id: string;
  status: string;
  total_amount: number;
  items: OrderItemRead[];
  carrier?: string | null;
  tracking_no?: string | null;
  shipping_events: Record<string, unknown>[];
}

export interface MessageLike {
  role?: string;
  type?: string;
  content?: unknown;
  [k: string]: unknown;
}

export interface ApprovalRead {
  approval_id: string;
  operation_id: string;
  thread_id: string;
  pending_action: string;
  status: string;
  order_id: string | null;
  amount: number | null;
  reason: string | null;
  created_at: number;
  approver: string | null;
  feedback: string | null;
}

export interface OperationRead {
  operation_id: string;
  thread_id: string;
  order_id: string;
  pending_action: string;
  status: string;
  idempotency_key: string;
  created_at: number;
  result: Record<string, unknown> | null;
}

// ---- 角色与认证（前端只展示/路由分发，授权边界由后端强制）----
export type Role = "customer" | "agent" | "admin" | "approver";

export interface AuthUser {
  token: string;
  tenantId: string;
  tenantName: string;
  userId: string;
  role: Role;
}
