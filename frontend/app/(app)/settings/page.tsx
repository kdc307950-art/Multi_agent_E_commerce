"use client";
// 成员与设置（仅 admin）：列出租户成员及其角色。权限由后端 /api/members 强制（非 admin 403）。
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { App, Card, List, Table, Tag, Typography } from "antd";
import { useAuth } from "../../providers";
import { listMembers } from "@/lib/api";
import type { MemberRead } from "@/lib/types";

const ROLE_LABEL: Record<string, string> = {
  customer: "客户",
  agent: "客服",
  admin: "管理员",
  approver: "审批人",
};
const ROLE_COLOR: Record<string, string> = {
  customer: "default",
  agent: "blue",
  admin: "red",
  approver: "purple",
};

export default function SettingsPage() {
  const { user } = useAuth();
  const { message } = App.useApp();
  const router = useRouter();
  const [members, setMembers] = useState<MemberRead[]>([]);

  useEffect(() => {
    if (!user) {
      router.push("/login");
      return;
    }
    if (user.role !== "admin") {
      message.warning("仅管理员可访问成员与设置。");
      router.replace("/workbench");
      return;
    }
    listMembers(user.token)
      .then(setMembers)
      .catch((e) => message.error(e instanceof Error ? e.message : String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  if (!user || user.role !== "admin") return null;

  return (
    <Card title={`成员与设置 · ${user.tenantName}`}>
      <Typography.Paragraph type="secondary">
        本页仅租户管理员可见（后端强制 admin 角色）。当前租户成员：
      </Typography.Paragraph>
      <Table<MemberRead>
        rowKey={(r) => `${r.tenant_id}:${r.user_id}`}
        dataSource={members}
        pagination={false}
        columns={[
          { title: "用户", dataIndex: "user_id" },
          { title: "tenant", dataIndex: "tenant_id" },
          {
            title: "角色",
            dataIndex: "role",
            render: (r: string) => <Tag color={ROLE_COLOR[r] ?? "default"}>{ROLE_LABEL[r] ?? r}</Tag>,
          },
          {
            title: "状态",
            dataIndex: "status",
            render: (s: string) => <Tag color={s === "active" ? "green" : "default"}>{s}</Tag>,
          },
        ]}
      />
    </Card>
  );
}
