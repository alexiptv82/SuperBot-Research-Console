import React from "react";
import "@/App.css";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "@/lib/auth";
import { LocaleProvider, useT } from "@/lib/locale";
import { AppShell } from "@/components/AppShell";
import LoginPage from "@/pages/Login";
import OverviewPage from "@/pages/Overview";
import UploadPage from "@/pages/Upload";
import RegistryPage from "@/pages/Registry";
import SessionDetailPage from "@/pages/SessionDetail";
import ReportsPage from "@/pages/Reports";
import CheckpointsPage from "@/pages/Checkpoints";
import PolicyPage from "@/pages/Policy";
import AuditPage from "@/pages/Audit";
import { Toaster } from "@/components/ui/sonner";

function Loader() {
  const t = useT();
  return (
    <div className="min-h-screen grid place-items-center text-sm text-muted-foreground">
      {t("common.loading")}
    </div>
  );
}

function Protected({ children }) {
  const { authenticated, loading } = useAuth();
  if (loading) return <Loader />;
  if (!authenticated) return <Navigate to="/login" replace />;
  return children;
}

function PublicOnly({ children }) {
  const { authenticated, loading } = useAuth();
  if (loading) return null;
  if (authenticated) return <Navigate to="/" replace />;
  return children;
}

export default function App() {
  React.useEffect(() => {
    const stored = localStorage.getItem("superbot.theme");
    const dark = stored !== "light";
    document.documentElement.classList.toggle("dark", dark);
    if (!stored) localStorage.setItem("superbot.theme", "dark");
  }, []);
  return (
    <div className="App">
      <BrowserRouter>
        <LocaleProvider>
          <AuthProvider>
            <Routes>
              <Route
                path="/login"
                element={
                  <PublicOnly>
                    <LoginPage />
                  </PublicOnly>
                }
              />
              <Route
                path="/"
                element={
                  <Protected>
                    <AppShell />
                  </Protected>
                }
              >
                <Route index element={<OverviewPage />} />
                <Route path="upload" element={<UploadPage />} />
                <Route path="registry" element={<RegistryPage />} />
                <Route path="session/:sessionId" element={<SessionDetailPage />} />
                <Route path="reports" element={<ReportsPage />} />
                <Route path="checkpoints" element={<CheckpointsPage />} />
                <Route path="policy" element={<PolicyPage />} />
                <Route path="audit" element={<AuditPage />} />
              </Route>
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
            <Toaster />
          </AuthProvider>
        </LocaleProvider>
      </BrowserRouter>
    </div>
  );
}
