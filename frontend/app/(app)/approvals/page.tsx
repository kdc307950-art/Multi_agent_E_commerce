"use client";
// 审批中心：待审批/已审批列表 + 二次确认 + 状态刷新 + operation 查询。
// 决策权限：admin/approver 可审批；agent/customer 只读（后端同时强制校验角色）。
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Divider,
  Input,
  List,
  Modal,
  Row,
  Space,
  Tag,
  Typography,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { useAuth } from "../../providers";
import { decideApproval, getOperation, listApprovals } from "@/lib/api";
import type { ApprovalRead, OperationRead } from "@/lib/types";

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
const STATUS_COLOR: Record<string, string> = {
  pending: "gold",
  approved: "green",
  rejected: "red",
  timeout: "blue",
};

export default function ApprovalsPage() {
  const { user } = useAuth();
  const { message } = App.useApp();
  const router = useRouter();
  const [approvals, setApprovals] = useState<ApprovalRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<ApprovalRead | null>(null);
  const [operation, setOperation] = useState<OperationRead | null>(null);
  const [confirming, setConfirming] = useState<{ approval: ApprovalRead; approved: boolean } | null>(null);
  const [feedback, setFeedback] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const canDecide = user?.role === "admin" || user?.role === "approver";

  const refresh = useCallback(async () => {
    if (!user) return;
    setLoading(true);
    try {
      setApprovals(await listApprovals(user.token));
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [user, message]);

  useEffect(() => {
    if (!user) router.push("/login");
    else refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  async function queryOperation(approval: ApprovalRead) {
    if (!user) return;
    try {
      const op = await getOperation(user.token, approval.operation_id);
      setOperation(op);
      setSelected(approval);
      message.success(`操作状态：${op.status}`);
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    }
  }

  async function submitDecision() {
    if (!user || !confirming) return;
    const { approval, approved } = confirming;
    if (!approved && !feedback.trim()) {
      message.warning("拒绝时必须填写原因。");
      return;
    }
    setSubmitting(true);
    try {
      const res = await decideApproval(
        user.token,
        approval.approval_id,
        approved,
        true,
        feedback.trim() || undefined,
        approval.operation_id,
        approval.pending_action,
      );
      message.success(res.message);
      setConfirming(null);
      setFeedback("");
      // 刷新列表并把选中的审批/操作更新为最新状态。
      const fresh = await listApprovals(user.token);
      setApprovals(fresh);
      setSelected(fresh.find((a) => a.approval_id === approval.approval_id) ?? approval);
      setOperation(await getOperation(user.token, approval.operation_id));
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  if (!user) return null;

  return (
    <Row gutter={16}>
      <Col span={10}>
        <Card
          title="审批中心"
          extra={
            <Button icon={<ReloadOutlined />} onClick={refresh} loading={loading}>
              刷新状态
            </Button>
          }
        >
          {!canDecide && (
            <Alert type="info" showIcon message="当前角色仅可查看，审批由 admin/approver 完成。" style={{ marginBottom: 12 }} />
          )}
          <List
            loading={loading}
            dataSource={approvals}
            locale={{ emptyText: "暂无审批单" }}
            renderItem={(a) => (
              <List.Item
                onClick={() => {
                  setSelected(a);
                  queryOperation(a);
                }}
                style={{ cursor: "pointer", background: selected?.approval_id === a.approval_id ? "#e6f4ff" : undefined }}
              >
                <div style={{ width: "100%" }}>
                  <Space>
                    <Tag color={ACTION_COLOR[a.pending_action]}>{ACTION_LABEL[a.pending_action] || a.pending_action}</Tag>
                    <Tag color={STATUS_COLOR[a.status] ?? "default"}>{a.status}</Tag>
                  </Space>
                  <div style={{ fontSize: 13, marginTop: 4 }}>
                    {a.order_id} {a.amount != null ? `¥${a.amount.toFixed(2)}` : ""}
                  </div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    审批 {a.approval_id.slice(0, 8)}… 操作 {a.operation_id.slice(0, 8)}… · {a.thread_id.slice(0, 8)}…
                  </Typography.Text>
                </div>
              </List.Item>
            )}
          />
        </Card>
      </Col>

      <Col span={14}>
        <Card title="审批详情">
          <Divider style={{ margin: "8px 0" }} />
          {selected ? (
            <Descriptions column={1} size="small" layout="vertical">
              <Descriptions.Item label="审批号">{selected.approval_id}</Descriptions.Item>
              <Descriptions.Item label="操作号 (operation_id)">{selected.operation_id}</Descriptions.Item>
              <Descriptions.Item label="会话 (thread_id)">{selected.thread_id}</Descriptions.Item>
              <Descriptions.Item label="动作">
                <Tag color={ACTION_COLOR[selected.pending_action]}>{ACTION_LABEL[selected.pending_action] || selected.pending_action}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="订单">{selected.order_id}</Descriptions.Item>
              <Descriptions.Item label="金额">{selected.amount != null ? `¥${selected.amount.toFixed(2)}` : "—"}</Descriptions.Item>
              <Descriptions.Item label="申请原因">{selected.reason ?? "—"}</Descriptions.Item>
              <Descriptions.Item label="当前状态">
                <Tag color={STATUS_COLOR[selected.status] ?? "default"}>{selected.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="审批人">{selected.approver ?? "（等待审批）"}</Descriptions.Item>
              <Descriptions.Item label="审批备注">{selected.feedback ?? "—"}</Descriptions.Item>
            </Descriptions>
          ) : (
            <Typography.Text type="secondary">选择左侧审批单查看详情。</Typography.Text>
          )}

          <Divider style={{ margin: "12px 0" }} />

          <Typography.Text strong>操作（operation）状态查询：</Typography.Text>
          {operation ? (
            <div style={{ marginTop: 8 }}>
              <Space wrap style={{ marginBottom: 8 }}>
                <Tag color={STATUS_COLOR[operation.status] ?? "default"}>{operation.status}</Tag>
                <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                  幂等键：{operation.idempotency_key}
                </Typography.Text>
              </Space>
              {operation.result ? (
                <pre style={{ background: "#fafafa", padding: 8, borderRadius: 4, fontSize: 12 }}>
                  {JSON.stringify(operation.result, null, 2)}
                </pre>
              ) : (
                <Typography.Text type="secondary" style={{ fontSize: 13 }}>（暂无执行结果）</Typography.Text>
              )}
            </div>
          ) : (
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>（点击列表项查询）</Typography.Text>
          )}

          {canDecide && selected && (
            <div style={{ marginTop: 16 }}>
              <Space>
                <Button type="primary" onClick={() => { setFeedback(""); setConfirming({ approval: selected, approved: true }); }}>
                  通过
                </Button>
                <Button danger onClick={() => { setFeedback(""); setConfirming({ approval: selected, approved: false }); }}>
                  拒绝
                </Button>
              </Space>
            </div>
          )}
        </Card>
      </Col>

      <Modal
        title={confirming?.approved ? "确认通过审批？" : "确认拒绝该申请？"}
        open={Boolean(confirming)}
        confirmLoading={submitting}
        onOk={submitDecision}
        onCancel={() => setConfirming(null)}
        okText={confirming?.approved ? "确认通过" : "确认拒绝"}
        okButtonProps={{ danger: Boolean(confirming && !confirming.approved) }}
      >
        <Typography.Paragraph>
          您正在{(confirming?.approved ? "通过" : "拒绝")}审批单{" "}
          <Typography.Text code>{confirming?.approval.approval_id.slice(0, 12)}…</Typography.Text>
          ，该操作将锚定到唯一操作号并记录审批人。
        </Typography.Paragraph>
        {!confirming?.approved && (
          <Input.TextArea
            placeholder="请填写拒绝原因（必填）"
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            rows={3}
          />
        )}
      </Modal>
    </Row>
  );
}
