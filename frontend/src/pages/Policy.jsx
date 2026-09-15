import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Copy, Check } from "lucide-react";

function Rule({ children }) {
  return <li className="text-xs leading-relaxed">{children}</li>;
}

export default function PolicyPage() {
  const [policy, setPolicy] = useState(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    api.get("/policy").then((r) => setPolicy(r.data));
  }, []);

  const copySha = async () => {
    if (!policy?.collector_sha256) return;
    await navigator.clipboard.writeText(policy.collector_sha256);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  if (!policy) return <div className="text-sm text-muted-foreground">Loading…</div>;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight">Project Policy</h1>
        <p className="text-sm text-muted-foreground">
          Frozen rules from the SuperBot Trading Project handoff. Later deterministic
          measurements supersede this document — but nothing here changes silently in V1.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-semibold tracking-wide">Frozen collector SHA256</CardTitle>
        </CardHeader>
        <CardContent>
          <div
            className="rounded-lg border bg-[hsl(var(--surface-2))] p-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3"
            data-testid="policy-collector-sha256"
          >
            <code className="font-mono text-xs break-all">{policy.collector_sha256}</code>
            <Button
              variant="secondary"
              size="sm"
              onClick={copySha}
              data-testid="policy-copy-sha256-button"
            >
              {copied ? <Check className="h-4 w-4 mr-1" /> : <Copy className="h-4 w-4 mr-1" />}
              {copied ? "Copied" : "Copy"}
            </Button>
          </div>
          <p className="text-[10px] text-muted-foreground mt-2">
            Every uploaded ZIP is compared against this hash. Mismatch → FAIL.
          </p>
        </CardContent>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card data-testid="policy-rules-block">
          <CardHeader>
            <CardTitle className="text-sm font-semibold tracking-wide">Frozen rules</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1.5 list-disc pl-5">
              {policy.frozen_rules.map((r, i) => (
                <Rule key={i}>{r}</Rule>
              ))}
            </ul>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-semibold tracking-wide">V1 prohibitions</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1.5 list-disc pl-5">
              {policy.prohibitions_v1.map((r, i) => (
                <Rule key={i}>{r}</Rule>
              ))}
            </ul>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-semibold tracking-wide">Verdict vocabulary</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1 font-mono text-xs">
              {policy.verdicts.map((v) => <li key={v}>{v}</li>)}
            </ul>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-semibold tracking-wide">Duplicate states</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1 font-mono text-xs">
              {policy.duplicate_states.map((v) => <li key={v}>{v}</li>)}
            </ul>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-semibold tracking-wide">Frozen horizons</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="font-mono text-xs flex flex-wrap gap-1">
              {policy.frozen_horizons.map((h) => (
                <span key={h} className="rounded border px-1.5 py-0.5">{h}</span>
              ))}
            </div>
            <div className="mt-2 text-[10px] text-muted-foreground">Economic hurdle: {policy.economic_hurdle_bps} bps</div>
            <div className="text-[10px] text-muted-foreground">Quantiles: {policy.quantiles.join(" · ")}</div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-semibold tracking-wide">FrozenAnalysisEngine</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="inline-flex items-center gap-2 rounded-md border px-2 py-0.5 text-xs font-medium text-muted-foreground">
            <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground" />
            {policy.engine.status}
          </div>
          <p className="mt-3 text-xs text-muted-foreground leading-relaxed">
            {policy.engine.reason}
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
