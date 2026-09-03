"use strict";
// 自定义 SSE 解析器与流式客户端：消费冻结的六种事件（accepted/token/node/approval_required/done/error）。
//
// 注意：Vercel AI SDK 默认 useChat 不能直接解析本协议；EventSource 不能提交 POST body。
// 因此使用 fetch + ReadableStream + 自定义 parser。事件以 (stream_id, id) 去重，防止重放重复。
//
// 断线恢复：唯一的自动网络动作是 resume（mode=resume + stream_id + Last-Event-ID），
// 它只重放持久化事件，绝不会重新执行图/工具/敏感写。敏感写不因断线/刷新重放。
import type { ChatResumeBody, ChatStartBody, SseFrame, SseEventType } from "./types";

const FROZEN_EVENTS: ReadonlySet<string> = new Set([
  "accepted",
  "token",
  "node",
  "approval_required",
  "done",
  "error",
]);

/** 逐帧解析一段 SSE 文本（用于单元测试/重放校验）。 */
export function parseSse(text: string): SseFrame[] {
  const frames: SseFrame[] = [];
  const blocks = text.split("\n\n");
  for (const block of blocks) {
    const trimmed = block.trim();
    if (!trimmed) continue;
    let id: number | undefined;
    let event: string | undefined;
    let data: Record<string, unknown> = {};
    for (const line of trimmed.split("\n")) {
      if (line.startsWith("id: ")) id = Number(line.slice(4));
      else if (line.startsWith("event: ")) event = line.slice(7);
      else if (line.startsWith("data: ")) data = JSON.parse(line.slice(6));
    }
    if (event !== undefined && FROZEN_EVENTS.has(event)) {
      frames.push({ id: id as number, event: event as SseEventType, data });
    }
  }
  return frames;
}

/** 从 fetch 流式响应读取 SSE 帧。成功帧逐个调用 onFrame。 */
export async function consumeSse(
  response: Response,
  onFrame: (frame: SseFrame) => void,
): Promise<void> {
  if (!response.body) throw new Error("no body");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const frames = parseSse(block);
      for (const f of frames) onFrame(f);
    }
  }
}

export type ChannelStatus = "idle" | "connecting" | "streaming" | "done" | "error" | "reconnecting";

export interface ChannelCallbacks {
  onFrame: (frame: SseFrame) => void;
  onStatus?: (status: ChannelStatus) => void;
  onError?: (message: string) => void;
}

// 去重窗口：保留最近 event id，防止 resume 重放重复 token/节点状态。
const MAX_DEDUP = 512;

/**
 * 单个 SSE 流通道：负责 start/resume、去重、断线恢复与取消。
 * 状态机见《前端体验与工作台设计》§5.2。
 */
export class SseChannel {
  streamId: string | null = null;
  lastId = 0;
  seen = new Set<number>();
  status: ChannelStatus = "idle";
  private controller: AbortController | null = null;
  private cb: ChannelCallbacks;

  constructor(cb: ChannelCallbacks) {
    this.cb = cb;
  }

  private setStatus(s: ChannelStatus) {
    this.status = s;
    this.cb.onStatus?.(s);
  }

  /** 去重：只处理未见过的 event id；紧凑窗口上限。 */
  private accept(frame: SseFrame): boolean {
    if (this.seen.has(frame.id)) return false;
    this.seen.add(frame.id);
    if (this.seen.size > MAX_DEDUP) {
      // 丢弃最旧，保留最近窗口。
      const sorted = Array.from(this.seen).sort((a, b) => a - b);
      const drop = sorted.slice(0, sorted.length - MAX_DEDUP);
      for (const d of drop) this.seen.delete(d);
    }
    this.lastId = frame.id;
    return true;
  }

  private handleFrames(frames: SseFrame[]) {
    for (const frame of frames) {
      if (!this.accept(frame)) continue;
      if (this.streamId == null && frame.event === "accepted") {
        this.streamId = (frame.data.stream_id as string) || null;
      }
      this.cb.onFrame(frame);
      if (frame.event === "done" || frame.event === "error") {
        this.setStatus(frame.event === "done" ? "done" : "error");
      }
    }
  }

  /** 发起新一轮（start）。必须携带 thread_id + client_request_id + message。 */
  async start(token: string, body: ChatStartBody): Promise<void> {
    this.seen.clear();
    this.lastId = 0;
    this.streamId = null;
    this.setStatus("connecting");
    this.controller = new AbortController();
    const resp = await fetch(`/api/chat`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      signal: this.controller.signal,
    });
    if (!resp.ok) throw new Error(`chat start ${resp.status}`);
    await consumeSse(resp, (f) => this.handleFrames([f]));
  }

  /** 断线自动恢复：resume + Last-Event-ID，只重放漏掉的事件。 */
  async resume(token: string, streamId: string, lastEventId: number): Promise<void> {
    this.setStatus("connecting");
    this.controller = new AbortController();
    const body: ChatResumeBody = { mode: "resume", stream_id: streamId };
    const resp = await fetch(`/api/chat`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "Last-Event-ID": String(lastEventId),
      },
      body: JSON.stringify(body),
      signal: this.controller.signal,
    });
    if (!resp.ok) throw new Error(`chat resume ${resp.status}`);
    await consumeSse(resp, (f) => this.handleFrames([f]));
  }

  cancel() {
    this.controller?.abort();
    this.controller = null;
    this.setStatus("idle");
  }
}
