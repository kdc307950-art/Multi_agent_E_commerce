"use client";
// (app) 路由组外壳：左侧角色化菜单 + 顶部当前用户。按角色渲染菜单（体验），后端强制授权。
import { useMemo, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { App, Button, Layout, Menu, Space, Tag, Typography } from "antd";
import { LogoutOutlined, UserOutlined } from "@ant-design/icons";
import { ROLE_LABEL, ROLE_MENUS } from "@/lib/auth";
import { useAuth } from "../providers";

const { Header, Content, Sider } = Layout;

export default function AppShell({ children }: { children: React.ReactNode }) {
  const { user, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const { modal } = App.useApp();
  const [collapsed, setCollapsed] = useState(false);

  const menus = useMemo(() => (user ? ROLE_MENUS[user.role] : []), [user]);

  if (!user) {
    return (
      <div style={{ padding: 24 }}>
        <Typography.Text>未登录。</Typography.Text>
        <Button type="primary" style={{ marginLeft: 8 }} onClick={() => router.push("/login")}>
          去登录
        </Button>
      </div>
    );
  }

  const selectedKey = menus.find((m) => pathname.startsWith(m.href))?.key ?? menus[0]?.key;

  const onLogout = () => {
    modal.confirm({
      title: "确认退出？",
      onOk: () => {
        logout();
        router.push("/login");
      },
    });
  };

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider collapsible collapsed={collapsed} onCollapse={setCollapsed} theme="dark">
        <div style={{ color: "#fff", padding: 16, fontWeight: 600 }}>
          {collapsed ? "售后" : "售后多智能体工单"}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          onClick={(e) => router.push(e.key as string)}
          items={menus.map((m) => ({ key: m.href, label: m.label }))}
        />
      </Sider>
      <Layout>
        <Header
          style={{ background: "#fff", display: "flex", alignItems: "center", justifyContent: "space-between", paddingInline: 16 }}
        >
          <Typography.Text strong>
            {user.tenantName} · {ROLE_LABEL[user.role]}
          </Typography.Text>
          <Space>
            <Tag icon={<UserOutlined />} color="blue">
              {user.userId}
            </Tag>
            <Button size="small" icon={<LogoutOutlined />} onClick={onLogout}>
              退出
            </Button>
          </Space>
        </Header>
        <Content style={{ padding: 16 }}>{children}</Content>
      </Layout>
    </Layout>
  );
}
