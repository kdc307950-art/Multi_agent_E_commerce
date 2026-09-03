"use strict";
import type { Metadata } from "next";
import { ConfigProvider, App as AntApp } from "antd";
import zhCN from "antd/locale/zh_CN";
import "antd/dist/reset.css";
import { AuthProvider } from "./providers";

export const metadata: Metadata = {
  title: "电商售后多智能体工单系统",
  description: "客服工作台 / 审批中心 / 客户会话（多租户，多角色）",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <ConfigProvider locale={zhCN}>
          <AntApp>
            <AuthProvider>{children}</AuthProvider>
          </AntApp>
        </ConfigProvider>
      </body>
    </html>
  );
}
