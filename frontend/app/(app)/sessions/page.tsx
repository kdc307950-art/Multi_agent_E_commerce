"use client";
// 会话历史：按角色查看（customer 仅本人，staff 全租户），点击查看消息。
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, Col, Row, Typography } from "antd";
import SessionListPanel from "@/components/SessionListPanel";
import { useAuth } from "../../providers";
import { getMessages } from "@/lib/api";
import { textOf } from "@/lib/text";

export default function SessionsPage() {
  const { user } = useAuth();
  const router = useRouter();
  const [threadId, setThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<unknown[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!user) router.push("/login");
  }, [user, router]);

  useEffect(() => {
    if (!user || !threadId) return;
    setLoading(true);
    getMessages(user.token, threadId)
      .then((r) => setMessages(r.messages ?? []))
      .catch(() => setMessages([]))
      .finally(() => setLoading(false));
  }, [user, threadId]);

  if (!user) return null;

  return (
    <Row gutter={16} style={{ minHeight: "calc(100vh - 80px)" }}>
      <Col span={8}>
        <Card styles={{ body: { height: "100%" } }}>
          <SessionListPanel token={user.token} activeThreadId={threadId} onSelect={setThreadId} />
        </Card>
      </Col>
      <Col span={16}>
        <Card title={`会话消息 ${threadId ? `(${threadId.slice(0, 8)}…)` : ""}`} loading={loading} style={{ height: "100%" }}>
          {!threadId ? (
            <Typography.Text type="secondary">选择会话查看消息历史。</Typography.Text>
          ) : messages.length === 0 ? (
            <Typography.Text type="secondary">暂无消息记录。</Typography.Text>
          ) : (
            messages.map((m, i) => {
              const role = (m as { role?: string; type?: string }).role;
              const content = textOf((m as { content?: unknown }).content);
              return (
                <div key={i} style={{ marginBottom: 8, padding: 8, background: "#fafafa", borderRadius: 4 }}>
                  <Typography.Text strong style={{ fontSize: 12, color: role === "user" ? "#1677ff" : "#555" }}>
                    {role || (m as { type?: string }).type || "msg"}
                  </Typography.Text>
                  <div style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>{content}</div>
                </div>
              );
            })
          )}
        </Card>
      </Col>
    </Row>
  );
}
