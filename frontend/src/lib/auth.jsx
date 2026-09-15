import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

const AuthCtx = createContext({
  authenticated: false,
  loading: true,
  login: async () => false,
  logout: async () => {},
  refresh: async () => {},
});

export function AuthProvider({ children }) {
  const [state, setState] = useState({ authenticated: false, loading: true });
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const r = await api.get("/auth/me");
      if (mounted.current) {
        setState({ authenticated: !!r.data?.authenticated, loading: false });
      }
    } catch (_) {
      if (mounted.current) setState({ authenticated: false, loading: false });
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Global listener for auth errors bubbled by axios interceptor.
  useEffect(() => {
    const orig = api.interceptors.response.use(
      (r) => r,
      (err) => {
        if (err?.isAuthError && mounted.current) {
          setState((s) => (s.authenticated ? { authenticated: false, loading: false } : s));
        }
        return Promise.reject(err);
      },
    );
    return () => api.interceptors.response.eject(orig);
  }, []);

  const login = async (password) => {
    try {
      await api.post("/auth/login", { password });
      // Re-verify with the server so we know the cookie is being echoed
      // back on subsequent requests before we flip authenticated=true.
      await refresh();
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

  return (
    <AuthCtx.Provider value={{ ...state, login, logout, refresh }}>{children}</AuthCtx.Provider>
  );
}

export function useAuth() {
  return useContext(AuthCtx);
}
