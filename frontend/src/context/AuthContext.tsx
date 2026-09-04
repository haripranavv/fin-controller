import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { checkSession, demoLogin as apiDemoLogin, getToken, login as apiLogin, logout as apiLogout, register as apiRegister, setToken } from "../api";

interface AuthContextValue {
  status: "checking" | "authenticated" | "unauthenticated";
  email: string | null;
  isDemo: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, displayName?: string) => Promise<void>;
  loginAsDemo: () => Promise<void>;
  logout: () => void;
  error: string | null;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthContextValue["status"]>("checking");
  const [email, setEmail] = useState<string | null>(null);
  const [isDemo, setIsDemo] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) { setStatus("unauthenticated"); return; }
    checkSession().then((s) => {
      if (s.valid) { setEmail(s.email); setIsDemo(s.is_demo); setStatus("authenticated"); }
      else { setToken(null); setStatus("unauthenticated"); }
    });
  }, []);

  const login = useCallback(async (e: string, password: string) => {
    setError(null);
    try {
      const res = await apiLogin(e, password);
      setToken(res.token);
      setEmail(res.email);
      setIsDemo(res.is_demo);
      setStatus("authenticated");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      throw err;
    }
  }, []);

  const register = useCallback(async (e: string, password: string, displayName?: string) => {
    setError(null);
    try {
      const res = await apiRegister(e, password, displayName);
      setToken(res.token);
      setEmail(res.email);
      setIsDemo(res.is_demo);
      setStatus("authenticated");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      throw err;
    }
  }, []);

  const loginAsDemo = useCallback(async () => {
    setError(null);
    try {
      const res = await apiDemoLogin();
      setToken(res.token);
      setEmail(res.email);
      setIsDemo(res.is_demo);
      setStatus("authenticated");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      throw err;
    }
  }, []);

  const logout = useCallback(() => {
    apiLogout();
    setToken(null);
    setEmail(null);
    setIsDemo(false);
    setStatus("unauthenticated");
  }, []);

  const value = useMemo(
    () => ({ status, email, isDemo, login, register, loginAsDemo, logout, error }),
    [status, email, isDemo, login, register, loginAsDemo, logout, error],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
