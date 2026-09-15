import React, { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { api } from "@/lib/api";
import {
  LayoutDashboard,
  Upload,
  Database,
  FileText,
  Target,
  ShieldCheck,
  ScrollText,
  LogOut,
  Moon,
  Sun,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, testId: "sidebar-nav-overview" },
  { to: "/upload", label: "Upload Sessions", icon: Upload, testId: "sidebar-nav-upload" },
  { to: "/registry", label: "Session Registry", icon: Database, testId: "sidebar-nav-registry" },
  { to: "/reports", label: "QA Reports", icon: FileText, testId: "sidebar-nav-reports" },
  { to: "/checkpoints", label: "Checkpoints", icon: Target, testId: "sidebar-nav-checkpoints" },
  { to: "/policy", label: "Project Policy", icon: ShieldCheck, testId: "sidebar-nav-policy" },
  { to: "/audit", label: "System / Audit Log", icon: ScrollText, testId: "sidebar-nav-audit" },
];

function useTheme() {
  const [dark, setDark] = useState(() => {
    if (typeof localStorage === "undefined") return true;
    return localStorage.getItem("superbot.theme") !== "light";
  });
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("superbot.theme", dark ? "dark" : "light");
  }, [dark]);
  return { dark, toggle: () => setDark((d) => !d) };
}

export function AppShell() {
  const { logout } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const { dark, toggle } = useTheme();
  const [overview, setOverview] = useState(null);

  useEffect(() => {
    api
      .get("/overview")
      .then((r) => setOverview(r.data))
      .catch(() => {});
  }, [loc.pathname]);

  const engineStatus = overview?.engine?.status || "NOT_CONFIGURED";
  const sessionCount = overview?.sessions ?? 0;

  return (
    <div className="min-h-screen w-full bg-background text-foreground grid grid-cols-1 md:grid-cols-[264px_1fr]">
      {/* Sidebar */}
      <aside className="hidden md:flex flex-col border-r bg-card">
        <div className="px-4 py-5 border-b">
          <Link to="/" className="flex items-center gap-2">
            <div className="h-8 w-8 rounded-md bg-[hsl(var(--focus))] grid place-items-center text-white font-bold font-mono">S</div>
            <div>
              <div className="text-sm font-semibold leading-tight">SuperBot</div>
              <div className="text-[10px] text-muted-foreground tracking-wider uppercase">Research Console V1</div>
            </div>
          </Link>
        </div>
        <nav className="flex-1 px-2 py-3 space-y-0.5">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.to === "/"}
              data-testid={n.testId}
              className={({ isActive }) =>
                cn(
                  "relative flex items-center gap-2 rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-accent hover:text-foreground transition-colors",
                  isActive && "bg-accent text-foreground before:absolute before:left-0 before:top-2 before:bottom-2 before:w-[2px] before:bg-[hsl(var(--focus))]",
                )
              }
            >
              <n.icon className="h-4 w-4" />
              <span>{n.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="px-4 py-3 border-t text-[10px] text-muted-foreground font-mono break-all">
          collector
          <br />
          {overview?.collector_sha256?.slice(0, 24) || "e924edc1b834…"}
        </div>
      </aside>

      {/* Main */}
      <div className="flex flex-col min-h-screen">
        <header className="h-14 border-b bg-background/80 backdrop-blur flex items-center justify-between px-4 sm:px-6">
          <div className="flex items-center gap-3">
            <div className="md:hidden text-sm font-semibold">SuperBot</div>
            <div className="text-xs text-muted-foreground">
              <span className="hidden sm:inline">Deterministic QA console — no trading, no exchange creds</span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span
              data-testid="topbar-session-count"
              className="hidden sm:inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium text-muted-foreground"
            >
              {sessionCount} sessions
            </span>
            <span
              data-testid="topbar-engine-status-pill"
              className="inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium text-muted-foreground"
              title="FrozenAnalysisEngine status"
            >
              <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground" />
              Engine: {engineStatus}
            </span>
            <Button
              variant="ghost"
              size="icon"
              onClick={toggle}
              aria-label="Toggle theme"
              data-testid="topbar-theme-toggle"
            >
              {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={async () => {
                await logout();
                nav("/login", { replace: true });
              }}
              data-testid="topbar-logout-button"
            >
              <LogOut className="h-4 w-4 mr-1" /> Logout
            </Button>
          </div>
        </header>

        {/* Mobile top nav */}
        <nav className="md:hidden flex overflow-x-auto border-b bg-card px-2">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.to === "/"}
              data-testid={`${n.testId}-mobile`}
              className={({ isActive }) =>
                cn(
                  "whitespace-nowrap px-3 py-2 text-xs text-muted-foreground border-b-2 border-transparent",
                  isActive && "text-foreground border-[hsl(var(--focus))]",
                )
              }
            >
              {n.label}
            </NavLink>
          ))}
        </nav>

        <main className="flex-1 px-4 sm:px-6 lg:px-8 py-6">
          <div className="max-w-[1400px] mx-auto">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
