"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { Spin } from "antd";
import { useAuth } from "./providers";
import { ROLE_HOME } from "@/lib/auth";

export default function Home() {
  const { user } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (user) {
      router.replace(ROLE_HOME[user.role] ?? "/chat");
    } else {
      router.replace("/login");
    }
  }, [user, router]);

  return (
    <div style={{ display: "flex", minHeight: "100vh", alignItems: "center", justifyContent: "center" }}>
      <Spin tip="正在进入系统…" />
    </div>
  );
}
