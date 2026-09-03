"use client";
// ConversationPanel：会话对话工作区。消费 SSE 六种事件，展示连接状态、node 进度、
// 审批提示、错误/人工升级提示，并提供输入框。写类异常的恢复始终先查操作/审批状态。
import { useMemo, useState } from "react";
import { Alert, Badge, Button, Card, Input, Space, Tag, Typography } from "antd";
import { useSseChat } from "@/lib/useSseChat";
import type { ChatMsg } from "@/lib/useSseChat";
import type { Role } from "@/lib/types";

const STATUS_MAP: Record<string, { color: string; label: string }> = {
  idle: { color: "default", label: "空闲" },
  connecting: { color: "processing", label: "连接中" },
  streaming: { color: "processing", label: "流式输出" },
  done: { color: "success", label: "完成" },
  error: { color: "error", label: "错误" },
  reconnecting: { color: "warning", label: "重连中" },
};

function Bubble({ msg }: { msg: ChatMsg }) {
  const isUser = msg.role === "user";
  return (
    <div style={{ display: "flex", justifyContent: isUser ? "flex-end" : "flex-start", marginBottom: 8 }}>
      <Card
        size="small"
        style={{
          maxWidth: "78%",
          background: isUser ? "#e6f4ff" : "#fff",
          borderRadius: 8,
        }}
        styles={{ body: { padding: "8px 12px" } }}
      >
        {!isUser && msg.nodes && msg.nodes.length > 0 && (
          <div style={{ marginBottom: 4 }}>
            <Tag color="processing" style={{ marginBottom: 4 }}>
              {msg.nodes.join(" → ")}
            </Tag>
          </div>
        )}
        <Typography.Paragraph style={{ marginBottom: 0, whiteSpace: "pre-wrap" }}>
          {msg.content || (msg.status === "connecting" ? "（等待回复）" : "")}
        </Typography.Paragraph>
        {msg.approval && (
          <div style={{ marginTop: 4 }}>
            <Tag color="gold">等待审批</Tag>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              审批号 {msg.approval.approval_id.slice(0, 8)}…
            </Typography.Text>
          </div>
        )}
        {msg.error && (
          <div style={{ marginTop: 4 }}>
            <Alert
              banner
              type={msg.handoff ? "warning" : "error"}
              message={msg.handoff ? "需要人工处理（已转人工/升级）" : "请求异常"}
              description={msg.error.message}
            />
          </div>
        )}
      </Card>
    </div>
  );
}

interface Props {
  token: string;
  threadId: string;
  role?: Role;
  onApprovalOpen?: () => void;
}

export default function ConversationPanel({ token, threadId, role, onApprovalOpen }: Props) {
  const [input, setInput] = useState("");
  const { messages, status, approval, error, send, reset } = useSseChat();
  const canSend = Boolean(token && threadId) && status !== "streaming" && status !== "connecting";

  const statusMeta = STATUS_MAP[status] ?? STATUS_MAP.idle;

  function onSend() {
    const text = input.trim();
    if (!text) return;
    setInput("");
    // client_request_id：同一次提交保持稳定，供服务端去重。
    send(token, threadId, crypto.randomUUID(), text);
  }

  return (
    <Card
      title={`对话工作区 ${threadId ? `(${threadId.slice(0, 8)}…)` : ""}`}
      styles={{ body: { height: "100%", display: "flex", flexDirection: "column", gap: 8 } }}
    >
      <Space split={<span />}>
        <Badge status={statusMeta.color as never} text={statusMeta.label} />
        {approval && (
          <Button size="small" type="link" onClick={() => onApprovalOpen?.()}>
            前往审批中心
          </Button>
        )}
        {error && <Tag color="error">{error.code ?? "error"}</Tag>}
      </Space>

      <div style={{ flex: 1, overflow: "auto", minHeight: 320, padding: 8 }}>
        {messages.length === 0 ? (
          <Typography.Text type="secondary">输入消息开始对话（政策/订单/退款/退货/改址）。</Typography.Text>
        ) : (
          messages.map((m) => <Bubble key={m.id} msg={m} />)
        )}
      </div>

      <Space.Compact style={{ width: "100%" }}>
        <Input
          disabled={!canSend}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onPressEnter={onSend}
          placeholder="例如：退货政策是什么？｜查订单 ORD-001｜我要退款，订单号 ORD-001"
        />
        <Button type="primary" onClick={onSend} disabled={!canSend} loading={status === "streaming" || status === "connecting"}>
          发送
        </Button>
      </Space.Compact>
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          SSE：断线自动恢复，仅对当前流续传；敏感写不因断线重放。
        </Typography.Text>
        {role && role !== "customer" && (
          <Button size="small" onClick={reset}>
            重置视图
          </Button>
        )}
      </div>
    </Card>
  );
}
