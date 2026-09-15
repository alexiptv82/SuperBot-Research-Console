import React, { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { useT, useLocale } from "@/lib/locale";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { VerdictBadge } from "@/components/VerdictBadge";
import { RefreshCcw } from "lucide-react";

const EVENTS = [
  "",
  "auth.login_success",
  "auth.login_failed",
  "auth.logout",
  "upload.qa",
  "upload.error",
  "session.reprocess",
  "session.checkpoint_assigned",
];

export default function AuditPage() {
  const t = useT();
  const { fmtDate } = useLocale();
  const [rows, setRows] = useState([]);
  const [q, setQ] = useState("");
  const [event, setEvent] = useState("");
  const [outcome, setOutcome] = useState("");

  const load = () => {
    const params = new URLSearchParams();
    if (event) params.set("event_type", event);
    if (outcome) params.set("outcome", outcome);
    api.get(`/audit?${params.toString()}`).then((r) => setRows(r.data?.events || []));
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [event, outcome]);

  const filtered = useMemo(() => {
    if (!q) return rows;
    const ql = q.toLowerCase();
    return rows.filter((r) => [r.session_id, r.qa_run_id, r.message, r.event_type, r.actor]
      .filter(Boolean).some((v) => String(v).toLowerCase().includes(ql)));
  }, [rows, q]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight">{t("audit.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("audit.subtitle")}</p>
      </div>

      <Card>
        <CardHeader className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <CardTitle className="text-sm font-semibold tracking-wide">
            {t("audit.events_count", { n: filtered.length })}
          </CardTitle>
          <div className="flex flex-col sm:flex-row gap-2">
            <Input placeholder={t("audit.search")} value={q} onChange={(e) => setQ(e.target.value)} className="sm:w-56" data-testid="audit-search" />
            <select value={event} onChange={(e) => setEvent(e.target.value)} className="rounded-md border bg-background px-2 py-1 text-xs" data-testid="audit-filter-event-type">
              {EVENTS.map((e) => <option key={e} value={e}>{e || t("audit.all_types")}</option>)}
            </select>
            <select value={outcome} onChange={(e) => setOutcome(e.target.value)} className="rounded-md border bg-background px-2 py-1 text-xs" data-testid="audit-filter-verdict">
              <option value="">{t("audit.all_outcomes")}</option>
              <option value="PASS">{t("verdict.PASS")}</option>
              <option value="PASS_WITH_WARNING">{t("verdict.PASS_WITH_WARNING")}</option>
              <option value="FAIL">{t("verdict.FAIL")}</option>
              <option value="UNRESOLVED">{t("verdict.UNRESOLVED")}</option>
            </select>
            <Button variant="secondary" onClick={load} data-testid="audit-refresh">
              <RefreshCcw className="h-4 w-4 mr-1" /> {t("audit.refresh")}
            </Button>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          <div data-testid="audit-log" className="divide-y max-h-[70vh] overflow-y-auto">
            {filtered.length === 0 && (
              <div className="px-4 py-8 text-center text-sm text-muted-foreground">{t("audit.empty")}</div>
            )}
            {filtered.map((r) => (
              <div key={r.id} data-testid="audit-log-row" className="grid grid-cols-1 sm:grid-cols-[170px_160px_1fr_auto] gap-3 px-4 py-2 hover:bg-accent/50">
                <div data-testid="audit-log-timestamp" className="font-mono text-[10px] text-muted-foreground">{fmtDate(r.ts)}</div>
                <div data-testid="audit-log-action" className="text-xs font-medium">{r.event_type}</div>
                <div className="text-xs">
                  <div>{r.message}</div>
                  {r.session_id && (<div className="font-mono text-[10px] text-muted-foreground">{r.session_id}</div>)}
                </div>
                <div data-testid="audit-log-outcome" className="flex items-center justify-end">
                  {r.outcome && ["PASS", "PASS_WITH_WARNING", "FAIL", "UNRESOLVED"].includes(r.outcome) ? (
                    <VerdictBadge verdict={r.outcome} size="sm" />
                  ) : r.outcome ? (
                    <span className="text-[10px] rounded border px-1.5 py-0.5 text-muted-foreground uppercase">{r.outcome}</span>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
