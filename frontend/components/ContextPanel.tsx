"use client";
// ContextPanel：右侧上下文 —— 订单/物流查询、政策/知识引用、审批状态、操作时间线。
import { useCallback, useEffect, useState } from "react";
import { Button, Card, Empty, Input, List, Space, Tag, Typography } from "antd";
import { SearchOutlined } from "@ant-design/icons";
import { getOrder, listApprovals } from "@/lib/api";
import type { ApprovalRead, OrderRead } from "@/lib/types";

const ACTION_LABEL: Record<string, string> = {
  refund: "退款",
  return_request: "退货",
  return_address: "改址",
};

const ACTION_COLOR: Record<string, string> = {
  refund: "volcano",
  return_request: "gold",
  return_address: "purple",
};

interface Props {
  token: string;
  threadId: string | null;
}

export default function ContextPanel({ token, threadId }: Props) {
  const [orderQuery, setOrderQuery] = useState("ORD-001");
  const [order, setOrder] = useState<OrderRead | null>(null);
  const [orderError, setOrderError] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<ApprovalRead[]>([]);
  const [loadingOrder, setLoadingOrder] = useState(false);

  const queryOrder = useCallback(async () => {
    if (!orderQuery.trim()) return;
    setLoadingOrder(true);
    setOrderError(null);
    try {
      const o = await getOrder(token, orderQuery.trim());
      setOrder(o);
    } catch (e) {
      setOrder(null);
      setOrderError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingOrder(false);
    }
  }, [token, orderQuery]);

  async function loadApprovals() {
    if (!threadId) return;
    try {
      const all = await listApprovals(token);
      setApprovals(all.filter((a) => a.thread_id === threadId));
    } catch {
      setApprovals([]);
    }
  }

  useEffect(() => {
    if (threadId) loadApprovals();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, threadId]);

  return (
    <Card title="上下文" styles={{ body: { display: "flex", flexDirection: "column", gap: 12 } }}>
      <div>
        <Typography.Text strong>订单 / 物流查询</Typography.Text>
        <Space.Compact style={{ width: "100%", marginTop: 6 }}>
          <Input
            value={orderQuery}
            onChange={(e) => setOrderQuery(e.target.value)}
            onPressEnter={queryOrder}
            placeholder="订单号，如 ORD-001"
          />
          <Button icon={<SearchOutlined />} loading={loadingOrder} onClick={queryOrder} />
        </Space.Compact>
        {orderError && <Typography.Text type="danger" style={{ fontSize: 12 }}>{orderError}</Typography.Text>}
        {order && (
          <div style={{ marginTop: 8, fontSize: 13 }}>
            <Tag color={order.status === "delivered" ? "green" : "blue"}>{order.status}</Tag>
            <div>金额：{order.total_amount?.toFixed(2)}</div>
            {order.items?.map((it, i) => (
              <div key={i} style={{ color: "#555" }}>
                {it.name} x{it.quantity}
              </div>
            ))}
            {order.tracking_no && <div>运单：{order.tracking_no}</div>}
            {order.shipping_events?.length ? (
              <ul style={{ paddingLeft: 16, margin: "4px 0", color: "#666" }}>
                {order.shipping_events.map((evt, i) => (
                  <li key={i}>{String(evt.status)}</li>
                ))}
              </ul>
            ) : null}
          </div>
        )}
      </div>

      <div>
        <Typography.Text strong>审批 / 操作</Typography.Text>
        <div style={{ marginTop: 6 }}>
          {approvals.length === 0 ? (
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              （本会话暂无审批/操作）
            </Typography.Text>
          ) : (
            <List
              size="small"
              dataSource={approvals}
              renderItem={(a) => (
                <List.Item style={{ padding: "6px 0" }}>
                  <div style={{ width: "100%", fontSize: 13 }}>
                    <Tag color={ACTION_COLOR[a.pending_action]}>{ACTION_LABEL[a.pending_action] || a.pending_action}</Tag>
                    <Tag color={a.status === "pending" ? "gold" : a.status === "approved" ? "green" : a.status === "rejected" ? "red" : "default"}>
                      {a.status}
                    </Tag>
                    <div style={{ color: "#666", fontSize: 12 }}>
                      {a.order_id} · {a.amount != null ? `¥${a.amount?.toFixed(2)}` : ""} · {a.approver ?? ""}
                    </div>
                  </div>
                </List.Item>
              )}
            />
          )}
        </div>
      </div>
    </Card>
  );
}
