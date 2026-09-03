"use strict";
// 统一 API client（只读配置，不传租户/user_id；身份由服务端认证决定）。
// SSE 端点使用 fetch 流式读取（见 lib/sse.ts）。路径统一 /api 前缀。
import type {
  ApprovalRead,
  ChatResumeBody,
  ChatStartBody,
  MemberRead,
  OperationRead,
  OrderRead,
  SessionRead,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api";

function authHeaders(token: string): HeadersInit {
  return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

export async function createSession(token: string): Promise<{ thread_id: string; session_id: string }> {
  const r = await fetch(`${BASE}/sessions`, { method: "POST", headers: authHeaders(token) });
  if (!r.ok) throw new Error(`createSession ${r.status}`);
  return r.json();
}

export async function listSessions(token: string): Promise<SessionRead[]> {
  const r = await fetch(`${BASE}/sessions`, { headers: authHeaders(token) });
  if (!r.ok) throw new Error(`listSessions ${r.status}`);
  return r.json();
}

export async function getMessages(token: string, threadId: string): Promise<{ messages: unknown[] }> {
  const r = await fetch(`${BASE}/sessions/${threadId}/messages`, { headers: authHeaders(token) });
  if (!r.ok) throw new Error(`getMessages ${r.status}`);
  return r.json();
}

export async function chatStart(
  token: string,
  body: ChatStartBody,
  signal?: AbortSignal,
): Promise<Response> {
  return fetch(`${BASE}/chat`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
    signal,
  });
}

export async function chatResume(
  token: string,
  body: ChatResumeBody,
  lastEventId: number,
): Promise<Response> {
  return fetch(`${BASE}/chat`, {
    method: "POST",
    headers: { ...authHeaders(token), "Last-Event-ID": String(lastEventId) },
    body: JSON.stringify(body),
  });
}

export async function listApprovals(token: string): Promise<ApprovalRead[]> {
  const r = await fetch(`${BASE}/approvals`, { headers: authHeaders(token) });
  if (!r.ok) throw new Error(`listApprovals ${r.status}`);
  return r.json();
}

export async function decideApproval(
  token: string,
  approvalId: string,
  approved: boolean,
  confirmation: boolean,
  feedback?: string,
  operationId?: string,
  pendingAction?: string,
): Promise<{ operation_id: string; status: string; message: string }> {
  const r = await fetch(`${BASE}/approvals/${approvalId}/decision`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ approved, confirmation, feedback, operation_id: operationId, pending_action: pendingAction }),
  });
  if (!r.ok) throw new Error(`decideApproval ${r.status}`);
  return r.json();
}

export async function getOperation(token: string, operationId: string): Promise<OperationRead> {
  const r = await fetch(`${BASE}/operations/${operationId}`, { headers: authHeaders(token) });
  if (!r.ok) throw new Error(`getOperation ${r.status}`);
  return r.json();
}

export async function getOrder(token: string, orderId: string): Promise<OrderRead> {
  const r = await fetch(`${BASE}/orders/${encodeURIComponent(orderId)}`, { headers: authHeaders(token) });
  if (!r.ok) throw new Error(`getOrder ${r.status}`);
  return r.json();
}

export async function listMembers(token: string): Promise<MemberRead[]> {
  const r = await fetch(`${BASE}/members`, { headers: authHeaders(token) });
  if (!r.ok) throw new Error(`listMembers ${r.status}`);
  return r.json();
}
