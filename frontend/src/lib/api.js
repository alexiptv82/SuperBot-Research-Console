import axios from "axios";

const BASE = process.env.REACT_APP_BACKEND_URL || "";

export const api = axios.create({
  baseURL: `${BASE}/api`,
  withCredentials: true,
  timeout: 60_000,
});

// Passive interceptor: attach a hint to the error so callers can react.
// We NEVER force a full-page redirect here — the AuthProvider is the
// single source of truth for the authentication state. Doing a
// window.location.href on any 401 caused a false "auto-logout" whenever
// a transient background request raced with cookie propagation.
api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err?.response?.status === 401) {
      err.isAuthError = true;
    }
    return Promise.reject(err);
  },
);
