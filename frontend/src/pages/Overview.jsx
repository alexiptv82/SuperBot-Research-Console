import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { useT, useLocale } from "@/lib/locale";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CheckpointRing } from "@/components/CheckpointRing";
import { VerdictBadge } from "@/components/VerdictBadge";
import { Button } from "@/components/ui/button";
import { ArrowRight, Sparkles, ShieldAlert } from "lucide-react";

export default function OverviewPage() {
  const [data, setData] = useState(null);
  const t = useT();
  const { fmtNumber } = useLocale();
  useEffect(() => {
    api.get("/overview").then((r) => setData(r.data)).catch(() => {});
  }, []);

  const cp = data?.checkpoints?.checkpoints || {};
  const ready = data?.checkpoints?.data_qa_ready;

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight">{t("overview.title")}</h1>
          <p className="text-sm text-muted-foreground">{t("overview.subtitle")}</p>
        </div>
        <div className="flex gap-2">
          <Button asChild variant="secondary" data-testid="overview-goto-upload">
            <Link to="/upload">
              <Sparkles className="h-4 w-4 mr-2" /> {t("overview.upload_cta")}
            </Link>
          </Button>
          <Button asChild variant="ghost" data-testid="overview-goto-registry">
            <Link to="/registry">
              {t("overview.registry_cta")} <ArrowRight className="h-4 w-4 ml-1" />
            </Link>
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-12 gap-4">
        <Card className="col-span-12 lg:col-span-7">
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-sm font-semibold tracking-wide">{t("overview.checkpoint_progress")}</CardTitle>
            <span
              data-testid="checkpoints-ready-flag"
              data-ready={ready ? "true" : "false"}
              className={`inline-flex items-center rounded-md border px-2 py-1 text-xs font-semibold ${
                ready
                  ? "border-[hsl(var(--verdict-pass)/0.35)] bg-[hsl(var(--verdict-pass-bg))] text-[hsl(var(--verdict-pass))]"
                  : "border-border bg-muted text-muted-foreground"
              }`}
            >
              {ready ? t("overview.ready_true") : t("overview.ready_false")}
            </span>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-4 place-items-center">
              {["OLD36", "NEW12", "TOTAL48", "NEW36", "TOTAL72"].map((k) => (
                <CheckpointRing
                  key={k}
                  label={k}
                  hours={cp[k]?.hours || 0}
                  target={cp[k]?.target || 0}
                />
              ))}
            </div>
          </CardContent>
        </Card>

        <Card className="col-span-12 lg:col-span-5">
          <CardHeader>
            <CardTitle className="text-sm font-semibold tracking-wide">{t("overview.verdict_distribution")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {["PASS", "PASS_WITH_WARNING", "FAIL", "UNRESOLVED"].map((v) => (
              <div key={v} className="flex items-center justify-between" data-testid={`overview-verdict-count-${v.toLowerCase()}`}>
                <VerdictBadge verdict={v} />
                <span className="font-mono tabular-nums text-sm">
                  {fmtNumber(data?.verdict_counts?.[v] ?? 0)}
                </span>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card className="col-span-12 lg:col-span-7">
          <CardHeader>
            <CardTitle className="text-sm font-semibold tracking-wide">{t("overview.recent_sessions")}</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {data?.recent?.length ? (
              <ul className="divide-y">
                {data.recent.map((r) => (
                  <li key={r.id} className="flex items-center gap-3 px-4 py-2 hover:bg-accent/50">
                    <VerdictBadge verdict={r.operational_status} size="sm" />
                    <div className="flex-1 min-w-0">
                      <div className="font-mono text-xs truncate">{r.session_id}</div>
                      <div className="text-[10px] text-muted-foreground truncate">{r.original_filename}</div>
                    </div>
                    <Link
                      className="text-xs text-[hsl(var(--focus))] hover:underline"
                      to={`/session/${encodeURIComponent(r.session_id)}`}
                      data-testid="overview-recent-view"
                    >
                      {t("common.view_arrow")}
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="px-4 py-8 text-center text-sm text-muted-foreground">
                {t("overview.recent_empty")}
              </div>
            )}
          </CardContent>
        </Card>

        <Card className="col-span-12 lg:col-span-5" data-testid="engine-status-card">
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-sm font-semibold tracking-wide">{t("overview.engine_title")}</CardTitle>
            <span
              className="inline-flex items-center gap-2 rounded-md border px-2 py-0.5 text-xs font-medium text-muted-foreground"
              data-testid="engine-status-pill"
            >
              <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground" />
              {data?.engine?.status || "NOT_CONFIGURED"}
            </span>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground leading-relaxed">
              {data?.engine?.reason ||
                "FrozenAnalysisEngine is intentionally NOT_CONFIGURED in V1."}
            </p>
            <div className="mt-4 flex items-center gap-2 text-xs text-muted-foreground">
              <ShieldAlert className="h-4 w-4" />
              <span>{t("overview.engine_note")}</span>
            </div>
            <Button disabled variant="secondary" className="mt-4" data-testid="engine-status-cta">
              {t("overview.engine_cta")}
            </Button>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
