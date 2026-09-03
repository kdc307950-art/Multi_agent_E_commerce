"use client";
// 客户会话视图：创建会话、会话列表（仅本人）、对话、订单/政策、审批状态。
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { App, Card, Col, Row, Typography } from "antd";
import ConversationPanel from "@/components/ConversationPanel";
import SessionListPanel from "@/components/SessionListPanel";
import ContextPanel from "@/components/ContextPanel";
import { useAuth } from "../../providers";
import { createSession } from "@/lib/api";

export default function ChatPage() {
  const { user } = useAuth();
  const { message } = App.useApp();
  const router = useRouter();
  const [threadId, setThreadId] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const onCreate = useCallback(async () => {
    if (!user) return;
    try {
      const s = await createSession(user.token);
      setThreadId(s.thread_id);
      message.success(`会话已创建（${s.thread_id.slice(0, 8)}…）`);
      setRefreshKey((k) => k + 1);
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    }
  }, [user, message]);

  useEffect(() => {
    if (!user) router.push("/login");
  }, [user, router]);

  if (!user) return null;

  return (
    <Row gutter={16} style={{ height: "calc(100vh - 80px)" }}>
      <Col span={6} style={{ height: "100%" }}>
        <Card styles={{ body: { height: "100%", overflow: "auto" } }}>
          <SessionListPanel
            token={user.token}
            activeThreadId={threadId}
            onSelect={setThreadId}
            onCreate={onCreate}
            refreshKey={refreshKey}
          />
        </Card>
      </Col>
      <Col span={12} style={{ height: "100%" }}>
        {threadId ? (
          <ConversationPanel token={user.token} threadId={threadId} role="customer" />
        ) : (
          <Card style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
            <Typography.Text type="secondary">新建或选择会话后开始。</Typography.Text>
          </Card>
        )}
      </Col>
      <Col span={6} style={{ height: "100%" }}>
        <ContextPanel token={user.token} threadId={threadId} />
      </Col>
    </Row>
  );
}
