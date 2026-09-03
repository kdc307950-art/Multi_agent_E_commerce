"use client";
import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Button, Card, Select, Space, Typography, Divider, Tag } from "antd";
import { useAuth } from "../providers";
import { IDENTITY_OPTIONS, ROLE_HOME, ROLE_LABEL } from "@/lib/auth";
import type { Role } from "@/lib/types";

function LoginInner() {
  const { login, user } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const [value, setValue] = useState<string>(IDENTITY_OPTIONS[0].label);

  const selected = IDENTITY_OPTIONS.find((o) => o.label === value)!;

  // 允许深链自动登录：/login?tenant=TENANT-A&user=ADMIN-A&role=admin&tenantName=租户A
  useEffect(() => {
    const tenant = params.get("tenant");
    const tenantName = params.get("tenantName");
    const userId = params.get("user");
    const role = params.get("role") as Role | null;
    if (tenant && userId && role && ["customer", "agent", "admin", "approver"].includes(role)) {
      login(tenant, tenantName || tenant, userId, role);
      router.replace(ROLE_HOME[role] ?? "/chat");
    }
  }, [params, login, router]);

  useEffect(() => {
    if (user && !params.get("role")) router.replace(ROLE_HOME[user.role]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  function onSubmit() {
    login(selected.tenantId, selected.tenantName, selected.userId, selected.role);
    router.push(`/chat`);
  }

  return (
    <div style={{ display: "flex", minHeight: "100vh", alignItems: "center", justifyContent: "center", background: "#f5f5f5" }}>
      <Card style={{ width: 460 }}>
        <Typography.Title level={4} style={{ marginTop: 0 }}>
          电商售后多智能体工单系统
        </Typography.Title>
        <Typography.Paragraph type="secondary">
          选择演示身份进入（本地 Mock 令牌；身份与数据边界由服务端强制）。
        </Typography.Paragraph>
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <div>
            <div style={{ marginBottom: 8 }}>演示身份</div>
            <Select
              style={{ width: "100%" }}
              value={value}
              onChange={setValue}
              options={IDENTITY_OPTIONS.map((o) => ({ label: o.label, value: o.label }))}
            />
          </div>
          <div>
            当前角色：
            <Tag color="blue">{ROLE_LABEL[selected.role]}</Tag>
            <Tag>{selected.tenantName}</Tag>
          </div>
          <Divider style={{ margin: "8px 0" }} />
          <Button type="primary" block onClick={onSubmit}>
            登录并进入客户会话
          </Button>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            各角色可访问菜单：customer→客户会话；agent/admin→客服工作台+会话历史；approver/admin→审批中心；admin→成员与设置。
          </Typography.Text>
        </Space>
      </Card>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginInner />
    </Suspense>
  );
}
