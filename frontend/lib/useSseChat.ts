"use client";
// useSseChat：驱动 POST /api/chat（SSE）的 React hook。
// - 事件去重由 SseChannel 负责（按事件 id）。
// - 断线后仅自动执行 resume（stream_id + Last-Event-ID），绝不重放敏感写。
// - 暴露连接状态、审批提示、错误/人工升级提示，供界面展示。
import { useCallback, useEffect, useRef, useState } from "react";
import { apiUrl } from "./api";
import { consumeSse, SseChannel, type ChannelStatus } from "./sse";
import type { SseFrame } from "./types";
import type { LifecycleStage } from "./types";
import type { LifecycleEvent } from "./types";

export interface ChatMsg {
  id: string;
  role: "user" | "assistant";
  content: string;
  status?: ChannelStatus;
  node?: string;
  nodes?: string[];
  approval?: { approval_id: string; operation_id: string; status: string } | null;
  error?: { code?: string; message: string; retryable: boolean; operation_id?: string } | null;
  handoff?: boolean;
  lifecycle?: LifecycleEvent[];
  traceId?: string;
}

export interface ApprovalHint {
  approvalId: string;
  operationId: string;
  status: string;
}

function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((b) => (typeof b === "string" ? b : (b?.text ?? "")))
      .join("");
  }
  return content ? String(content) : "";
}

export function useSseChat() {
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [status, setStatus] = useState<ChannelStatus>("idle");
  const [approval, setApproval] = useState<ApprovalHint | null>(null);
  const [error, setError] = useState<{ code?: string; message: string } | null>(null);
  const channelRef = useRef<SseChannel | null>(null);
  const traceControllersRef = useRef<Map<string, AbortController>>(new Map());
  const activeIdRef = useRef<string>("");

  const updateMessage = useCallback((messageId: string,
    patch: Partial<ChatMsg> | ((m: ChatMsg) => Partial<ChatMsg>)) => {
    setMessages((prev) => {
      const out = [...prev];
      const idx = out.findIndex((m) => m.id === messageId);
      if (idx < 0) return out;
      const p = typeof patch === "function" ? patch(out[idx]) : patch;
      out[idx] = { ...out[idx], ...p };
      return out;
    });
  }, []);

  const update = useCallback((patch: Partial<ChatMsg> | ((m: ChatMsg) => Partial<ChatMsg>)) => {
    updateMessage(activeIdRef.current, patch);
  }, [updateMessage]);

  const onStatus = useCallback((s: ChannelStatus) => {
    setStatus(s);
  }, []);

  const subscribeTrace = useCallback((token: string, traceId: string, messageId: string,
    initialSeq: number) => {
    if (traceControllersRef.current.has(traceId)) return;
    const controller = new AbortController();
    traceControllersRef.current.set(traceId, controller);

    void (async () => {
      let cursor = initialSeq;
      let terminal = false;
      while (!controller.signal.aborted && !terminal) {
        try {
          const response = await fetch(
            apiUrl(`/traces/${encodeURIComponent(traceId)}/events?after_seq=${cursor}&wait_seconds=20`),
            { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal },
          );
          if (!response.ok) {
            if ([401, 403, 404].includes(response.status)) return;
            throw new Error(`trace subscribe ${response.status}`);
          }
          await consumeSse(response, (frame) => {
            cursor = Math.max(cursor, frame.id);
            const stage = lifecycleStageOf(frame);
            if (!stage) return;
            const next = lifecycleEventOf(frame, traceId, stage);
            updateMessage(messageId, (message) => ({
              traceId,
              lifecycle: upsertLifecycle(message.lifecycle, next),
              status: stage === "shadow_completed" ? "done" : message.status,
              approval: stage === "shadow_completed"
                ? (message.approval ? { ...message.approval, status: "approved" } : message.approval)
                : stage === "approval_required" && next.status === "rejected"
                  ? (message.approval ? { ...message.approval, status: "rejected" } : message.approval)
                  : message.approval,
            }));
            if (stage === "shadow_completed" || stage === "human_handoff" ||
              (stage === "approval_required" && next.status === "rejected")) {
              terminal = true;
              setApproval((current) => current?.operationId === next.operation_id ? null : current);
              setStatus(stage === "human_handoff" ? "error" : "done");
            }
          });
        } catch (e) {
          if (controller.signal.aborted) return;
          await new Promise((resolve) => window.setTimeout(resolve, 750));
        }
      }
    })().finally(() => {
      traceControllersRef.current.delete(traceId);
    });
  }, [updateMessage]);

  const send = useCallback(
    async (token: string, threadId: string, clientRequestId: string, text: string) => {
      setError(null);
      setApproval(null);
      const now = Date.now();
      const userMsg: ChatMsg = { id: `u-${now}`, role: "user", content: text, status: "connecting" };
      const asstMsg: ChatMsg = {
        id: `a-${now}`,
        role: "assistant",
        content: "",
        status: "connecting",
        nodes: [],
      };
      setMessages((prev) => [...prev, userMsg, asstMsg]);
      activeIdRef.current = asstMsg.id;

      const ch = new SseChannel({
        onFrame: (frame: SseFrame) => {
          if (frame.data.trace_id) update({ traceId: String(frame.data.trace_id) });
          if (frame.event === "token") {
            update((m) => ({ content: (m.content || "") + String(frame.data.delta ?? "") }));
          } else if (frame.event === "node") {
            const lifecycle = frame.data.lifecycle as LifecycleStage | undefined;
            update((m) => {
              const name = String(frame.data.name ?? "");
              const st = String(frame.data.status ?? "");
              const nodes = m.nodes ? [...m.nodes] : [];
              if (st === "running" && !nodes.includes(name)) nodes.push(name);
              const history = m.lifecycle ? [...m.lifecycle] : [];
              if (lifecycle) {
                const next: LifecycleEvent = {
                  stage: lifecycle,
                  name: name || undefined,
                  status: st || undefined,
                  trace_id: frame.data.trace_id ? String(frame.data.trace_id) : m.traceId,
                  timestamp: typeof frame.data.timestamp === "number" ? frame.data.timestamp : undefined,
                  duration_ms: typeof frame.data.duration_ms === "number" ? frame.data.duration_ms : undefined,
                  tool: frame.data.tool ? String(frame.data.tool) : undefined,
                  reason: frame.data.reason ? String(frame.data.reason) : undefined,
                };
                const existing = history.findIndex((item) => item.stage === lifecycle);
                if (existing >= 0) history[existing] = { ...history[existing], ...next };
                else history.push(next);
              }
              return { node: name, nodes, lifecycle: history,
                traceId: frame.data.trace_id ? String(frame.data.trace_id) : m.traceId };
            });
          } else if (frame.event === "approval_required") {
            const aid = String(frame.data.approval_id ?? "");
            const oid = String(frame.data.operation_id ?? "");
            const traceId = String(frame.data.trace_id ?? "");
            const a = { approvalId: aid, operationId: oid, status: String(frame.data.status ?? "pending") };
            setApproval(a);
            update((m) => ({ approval: { approval_id: aid, operation_id: oid, status: a.status } }));
            update((m) => ({ lifecycle: upsertLifecycle(m.lifecycle, {
              stage: "approval_required", status: "waiting_approval",
              trace_id: frame.data.trace_id ? String(frame.data.trace_id) : m.traceId,
              timestamp: typeof frame.data.timestamp === "number" ? frame.data.timestamp : undefined,
              operation_id: oid, approval_id: aid,
            }) }));
            if (traceId) subscribeTrace(token, traceId, asstMsg.id, frame.id);
          } else if (frame.event === "done") {
            const fr = frame.data.final_response ? textOf(frame.data.final_response) : "";
            update((m) => ({ content: m.content || fr, status: "done", node: undefined }));
          } else if (frame.event === "error") {
            const e = {
              code: String(frame.data.code ?? ""),
              message: textOf(frame.data.message) || "请求异常",
              retryable: Boolean(frame.data.retryable),
              operation_id: frame.data.operation_id ? String(frame.data.operation_id) : undefined,
            };
            setError({ code: e.code, message: e.message });
            update({ status: "error", error: e, handoff: !e.retryable });
            if (!e.retryable) update((m) => ({ lifecycle: upsertLifecycle(m.lifecycle, {
              stage: "human_handoff", status: "transferred_to_human",
              reason: e.message, trace_id: m.traceId,
            }) }));
          }
        },
        onStatus,
        onError: (msg) => setError({ message: msg }),
      });
      channelRef.current = ch;

      try {
        await ch.start(token, {
          mode: "start",
          thread_id: threadId,
          client_request_id: clientRequestId,
          message: text,
        });
      } catch (e) {
        const msgText = e instanceof Error ? e.message : String(e);
        // 网络中断：若已建立 stream_id，唯一自动动作是 resume。
        if (ch.streamId && ch.lastId > 0) {
          try {
            await ch.resume(token, ch.streamId, ch.lastId);
            return;
          } catch {
            /* fallthrough */
          }
        }
        setError({ message: msgText });
        update((m) => ({ status: "error", error: { message: msgText, retryable: false, code: "network" } }));
      }
    },
    [update, onStatus, subscribeTrace],
  );

  const reset = useCallback(() => {
    channelRef.current?.cancel();
    for (const controller of traceControllersRef.current.values()) controller.abort();
    traceControllersRef.current.clear();
    setMessages([]);
    setApproval(null);
    setError(null);
    setStatus("idle");
  }, []);

  useEffect(() => () => {
    channelRef.current?.cancel();
    for (const controller of traceControllersRef.current.values()) controller.abort();
    traceControllersRef.current.clear();
  }, []);

  return { messages, status, approval, error, send, reset };
}

function lifecycleStageOf(frame: SseFrame): LifecycleStage | undefined {
  const value = frame.data.lifecycle ?? frame.data.stage;
  const allowed: LifecycleStage[] = [
    "route_started", "crewai_started", "tool_selected", "tool_validated",
    "approval_required", "shadow_started", "shadow_completed", "human_handoff",
  ];
  return typeof value === "string" && allowed.includes(value as LifecycleStage)
    ? value as LifecycleStage
    : undefined;
}

function lifecycleEventOf(frame: SseFrame, traceId: string, stage: LifecycleStage): LifecycleEvent {
  return {
    stage,
    name: frame.data.name ? String(frame.data.name) : undefined,
    status: frame.data.status ? String(frame.data.status) : undefined,
    trace_id: traceId,
    timestamp: typeof frame.data.timestamp === "number" ? frame.data.timestamp : undefined,
    duration_ms: typeof frame.data.duration_ms === "number" ? frame.data.duration_ms : undefined,
    tool: frame.data.tool ? String(frame.data.tool) : undefined,
    operation_id: frame.data.operation_id ? String(frame.data.operation_id) : undefined,
    approval_id: frame.data.approval_id ? String(frame.data.approval_id) : undefined,
  };
}

function upsertLifecycle(items: LifecycleEvent[] | undefined, next: LifecycleEvent): LifecycleEvent[] {
  const out = items ? [...items] : [];
  const idx = out.findIndex((item) => item.stage === next.stage);
  if (idx >= 0) out[idx] = { ...out[idx], ...next };
  else out.push(next);
  return out;
}
