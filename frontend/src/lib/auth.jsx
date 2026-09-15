import React, { createContext, useContext, useEffect, useState } from "react";
import { api } from "@/lib/api";

const AuthCtx = createContext({ authenticated: false, loading: true, login: async () => false, logout: async () => {} });

export function AuthProvider({ children }) {
  const [state, setState] = useState({ authenticated: false, loading: true });

  useEffect(() => {
    let alive = true;
    api
      .get("/auth/me")
      .then((r) => alive && setState({ authenticated: !!r.data?.authenticated, loading: false }))
      .catch(() => alive && setState({ authenticated: false, loading: false }));
    return () => {
      alive = false;
    };
  }, []);

  const login = async (password) => {
    try {
      await api.post("/auth/login", { password });
      setState({ authenticated: true, loading: false });
      return true;
    } catch (e) {
      setState({ authenticated: false, loading: false });
      return false;
    }
  };

  const logout = async () => {
    try {
      await api.post("/auth/logout");
    } catch (_) {}
    setState({ authenticated: false, loading: false });
  };

  return <AuthCtx.Provider value={{ ...state, login, logout }}>{children}</AuthCtx.Provider>;
}

export function useAuth() {
  return useContext(AuthCtx);
}
