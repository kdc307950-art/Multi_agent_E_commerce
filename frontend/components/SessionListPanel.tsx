"use client";
// SessionListPanel：会话/工单列表面板。customer 仅显示本人会话（后端强制），staff 显示租户内全部。
import { useEffect, useState } from "react";
import { Button, Empty, List, Space, Tag, Typography } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { listSessions } from "@/lib/api";
import type { SessionRead } from "@/lib/types";

interface Props {
  token: string;
  activeThreadId: string | null;
  onSelect: (threadId: string) => void;
  onCreate?: () => void;
  refreshKey?: number;
}

export default function SessionListPanel({ token, activeThreadId, onSelect, onCreate, refreshKey = 0 }: Props) {
  const [sessions, setSessions] = useState<SessionRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const list = await listSessions(token);
      setSessions(list);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (token) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, refreshKey]);

  const fmt = (t: number) => new Date(t * 1000).toLocaleString();

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <Typography.Text strong>会话 / 工单</Typography.Text>
        <Space>
          <Button size="small" icon={<ReloadOutlined />} onClick={load} />
          {onCreate && (
            <Button size="small" type="primary" onClick={onCreate}>
              新建会话
            </Button>
          )}
        </Space>
      </div>
      {error && <Typography.Text type="danger">{error}</Typography.Text>}
      <List
        size="small"
        loading={loading}
        dataSource={sessions}
        locale={{ emptyText: <Empty description="暂无会话" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        renderItem={(s) => (
          <List.Item
            onClick={() => onSelect(s.thread_id)}
            style={{
              cursor: "pointer",
              background: s.thread_id === activeThreadId ? "#e6f4ff" : undefined,
              padding: "8px 12px",
            }}
          >
            <div style={{ width: "100%" }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <Typography.Text strong style={{ fontSize: 13 }}>
                  {s.title || s.thread_id.slice(0, 8) + "…"}
                </Typography.Text>
                <Tag color={s.status === "active" ? "green" : "default"}>{s.status}</Tag>
              </div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {s.user_id ?? ""} · 消息 {s.message_count} · {fmt(s.created_at)}
              </Typography.Text>
            </div>
          </List.Item>
        )}
      />
    </div>
  );
}
