"use client";
// AuthProvider：从前端视角展示当前登录角色/租户（展示用途），并分发 token 到各页面。
// 后端仍通过 TenantContext 强制授权；前端不得提交 tenant_id/user_id。
import { createContext, useContext, useEffect, useState, useCallback } from "react";
import type { AuthUser } from "@/lib/types";
import { clearAuth, loadAuth, makeToken, saveAuth } from "@/lib/auth";

interface AuthContextValue {
  user: AuthUser | null;
  login: (tenantId: string, tenantName: string, userId: string, role: AuthUser["role"]) => void;
  logout: () => void;
  setRole: (role: AuthUser["role"]) => void;
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  login: () => {},
  logout: () => {},
  setRole: () => {},
});

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);

  useEffect(() => {
    setUser(loadAuth());
  }, []);

  const login = useCallback(
    (tenantId: string, tenantName: string, userId: string, role: AuthUser["role"]) => {
      const u: AuthUser = { token: makeToken(tenantId, userId, role), tenantId, tenantName, userId, role };
      saveAuth(u);
      setUser(u);
    },
    [],
  );

  const logout = useCallback(() => {
    clearAuth();
    setUser(null);
  }, []);

  const setRole = useCallback((role: AuthUser["role"]) => {
    setUser((prev) => {
      if (!prev) return prev;
      const u: AuthUser = {
        token: makeToken(prev.tenantId, prev.userId, role),
        tenantId: prev.tenantId,
        tenantName: prev.tenantName,
        userId: prev.userId,
        role,
      };
      saveAuth(u);
      return u;
    });
  }, []);

  return (
    <AuthContext.Provider value={{ user, login, logout, setRole }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
