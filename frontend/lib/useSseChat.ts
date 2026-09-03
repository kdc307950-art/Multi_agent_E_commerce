"use client";
// useSseChat：驱动 POST /api/chat（SSE）的 React hook。
// - 事件去重由 SseChannel 负责（按事件 id）。
// - 断线后仅自动执行 resume（stream_id + Last-Event-ID），绝不重放敏感写。
// - 暴露连接状态、审批提示、错误/人工升级提示，供界面展示。
import { useCallback, useRef, useState } from "react";
import { SseChannel, type ChannelStatus } from "./sse";
import type { SseFrame } from "./types";

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
  const activeIdRef = useRef<string>("");

  const update = useCallback((patch: Partial<ChatMsg> | ((m: ChatMsg) => Partial<ChatMsg>)) => {
    setMessages((prev) => {
      const out = [...prev];
      const idx = out.findIndex((m) => m.id === activeIdRef.current);
      if (idx < 0) return out;
      const p = typeof patch === "function" ? patch(out[idx]) : patch;
      out[idx] = { ...out[idx], ...p };
      return out;
    });
  }, []);

  const onStatus = useCallback((s: ChannelStatus) => {
    setStatus(s);
  }, []);

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
          if (frame.event === "token") {
            update((m) => ({ content: (m.content || "") + String(frame.data.delta ?? "") }));
          } else if (frame.event === "node") {
            update((m) => {
              const name = String(frame.data.name ?? "");
              const st = String(frame.data.status ?? "");
              const nodes = m.nodes ? [...m.nodes] : [];
              if (st === "running" && !nodes.includes(name)) nodes.push(name);
              return { node: name, nodes };
            });
          } else if (frame.event === "approval_required") {
            const aid = String(frame.data.approval_id ?? "");
            const oid = String(frame.data.operation_id ?? "");
            const a = { approvalId: aid, operationId: oid, status: String(frame.data.status ?? "pending") };
            setApproval(a);
            update((m) => ({ approval: { approval_id: aid, operation_id: oid, status: a.status } }));
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
    [update, onStatus],
  );

  const reset = useCallback(() => {
    channelRef.current?.cancel();
    setMessages([]);
    setApproval(null);
    setError(null);
    setStatus("idle");
  }, []);

  return { messages, status, approval, error, send, reset };
}
