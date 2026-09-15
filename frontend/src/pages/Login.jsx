import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Eye, EyeOff, Lock } from "lucide-react";

export default function LoginPage() {
  const { login } = useAuth();
  const nav = useNavigate();
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    const ok = await login(password);
    setBusy(false);
    if (ok) nav("/", { replace: true });
    else setError("Invalid password. Access denied.");
  };

  return (
    <div className="min-h-screen w-full grid place-items-center bg-background p-4">
      <div className="w-full max-w-md rounded-xl border bg-card p-6 shadow-[0_1px_0_hsl(var(--border))]">
        <div className="flex items-center gap-2 mb-6">
          <div className="h-9 w-9 rounded-md bg-[hsl(var(--focus))] grid place-items-center text-white font-bold font-mono">S</div>
          <div>
            <div className="text-sm font-semibold">SuperBot</div>
            <div className="text-[10px] text-muted-foreground tracking-wider uppercase">Research Console V1</div>
          </div>
        </div>
        <h1 className="text-lg font-semibold">Owner sign-in</h1>
        <p className="text-xs text-muted-foreground mt-1">
          This is a single-owner deterministic QA console. Uploads and results are logged to an
          immutable audit trail.
        </p>
        <form onSubmit={submit} className="mt-5 space-y-3">
          <div>
            <Label htmlFor="password" className="text-xs">Password</Label>
            <div className="relative">
              <Input
                id="password"
                type={show ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                data-testid="login-password-input"
                className="pr-10"
                autoFocus
                autoComplete="current-password"
              />
              <button
                type="button"
                onClick={() => setShow((s) => !s)}
                aria-label={show ? "Hide password" : "Show password"}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              >
                {show ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </div>
          {error && (
            <div data-testid="login-error-message" className="text-xs text-[hsl(var(--verdict-fail))]">
              {error}
            </div>
          )}
          <Button
            type="submit"
            className="w-full"
            disabled={busy || !password}
            data-testid="login-submit-button"
          >
            <Lock className="h-4 w-4 mr-2" /> {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>
        <div className="mt-6 text-[10px] text-muted-foreground leading-relaxed">
          Access is protected by a signed session cookie. The password is provisioned via the
          <span className="font-mono"> SUPERBOT_PASSWORD </span> environment variable. Change it before
          any production deployment.
        </div>
      </div>
    </div>
  );
}
