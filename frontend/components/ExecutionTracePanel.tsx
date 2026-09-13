"use client";

import { Badge, Space, Tag, Typography } from "antd";
import type { LifecycleEvent, LifecycleStage, Role } from "@/lib/types";

const FLOW: Array<{ stage: LifecycleStage; label: string }> = [
  { stage: "route_started", label: "路由识别" },
  { stage: "crewai_started", label: "CrewAI 启动" },
  { stage: "tool_selected", label: "工具选择" },
  { stage: "tool_validated", label: "工具校验" },
  { stage: "approval_required", label: "人工审批" },
  { stage: "shadow_started", label: "Shadow 执行" },
  { stage: "shadow_completed", label: "执行完成" },
  { stage: "human_handoff", label: "转人工" },
];

function stateOf(item?: LifecycleEvent) {
  if (!item) return { color: "default", text: "未开始" };
  if (item.status === "waiting_approval" || item.status === "pending") return { color: "warning", text: "等待中" };
  if (item.status === "transferred_to_human") return { color: "warning", text: "已转人工" };
  if (item.status === "failed") return { color: "error", text: "失败" };
  if (item.status === "running" || item.status === "started") return { color: "processing", text: "执行中" };
  return { color: "success", text: "已完成" };
}

function shortId(value?: string) {
  return value ? `${value.slice(0, 10)}…` : "";
}

export default function ExecutionTracePanel({ lifecycle, traceId, role }: {
  lifecycle?: LifecycleEvent[];
  traceId?: string;
  role?: Role;
}) {
  if (!lifecycle?.length && !traceId) return null;
  const byStage = new Map((lifecycle ?? []).map((item) => [item.stage, item]));
  const visible = role === "customer" ? FLOW.filter(({ stage }) => stage !== "crewai_started") : FLOW;
  return (
    <div style={{ margin: "0 0 10px", padding: "10px 0 2px", borderTop: "1px solid #f0f0f0" }}>
      <Space size={8} wrap style={{ marginBottom: 8 }}>
        <Typography.Text strong style={{ fontSize: 12 }}>执行轨迹</Typography.Text>
        {traceId && <Tag bordered={false}>Trace {shortId(traceId)}</Tag>}
      </Space>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(122px, 1fr))", gap: 6 }}>
        {visible.map(({ stage, label }, index) => {
          const item = byStage.get(stage);
          const state = stateOf(item);
          return (
            <div key={stage} style={{ position: "relative", minHeight: 58, padding: "7px 8px", border: "1px solid #e8e8e8", borderRadius: 6, background: item ? "#fafcff" : "#fff" }}>
              <Badge status={state.color as never} text={<span style={{ fontSize: 12 }}>{label}</span>} />
              <Typography.Text type="secondary" style={{ display: "block", fontSize: 11, marginTop: 4 }}>
                {item?.duration_ms != null ? `${item.duration_ms} ms` : state.text}
                {item?.tool && role !== "customer" ? ` · ${item.tool}` : ""}
              </Typography.Text>
              {item?.operation_id && role !== "customer" && (
                <Typography.Text type="secondary" style={{ display: "block", fontSize: 10 }}>操作 {shortId(item.operation_id)}</Typography.Text>
              )}
              {index < visible.length - 1 && <span aria-hidden="true" style={{ position: "absolute", right: -7, top: 27, color: "#bfbfbf", zIndex: 1 }}>›</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
