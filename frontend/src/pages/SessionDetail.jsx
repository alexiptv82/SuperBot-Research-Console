import React, { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { VerdictBadge } from "@/components/VerdictBadge";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import { ArrowLeft, Download, RefreshCcw } from "lucide-react";

const BACKEND = process.env.REACT_APP_BACKEND_URL || "";

function KV({ label, value, mono = false }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-[220px_1fr] gap-x-6 gap-y-1 py-1 border-b last:border-b-0">
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div className={mono ? "font-mono text-xs break-all" : "text-xs"}>{value ?? "—"}</div>
    </div>
  );
}

export default function SessionDetailPage() {
  const { sessionId } = useParams();
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();

  const load = () => api.get(`/sessions/${encodeURIComponent(sessionId)}`).then((r) => setData(r.data));
  useEffect(() => {
    load();
  }, [sessionId]);

  if (!data) return <div className="text-sm text-muted-foreground">Loading…</div>;
  const runs = data.qa_runs || [];
  const latest = runs[0];

  const reprocess = async () => {
    setBusy(true);
    try {
      await api.post(`/sessions/${encodeURIComponent(sessionId)}/reprocess`);
      await load();
    } catch (e) {
      alert(e?.response?.data?.detail || "Reprocess failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6" data-testid="session-detail-header">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="space-y-1">
          <button onClick={() => nav(-1)} className="text-xs text-muted-foreground hover:underline flex items-center gap-1">
            <ArrowLeft className="h-3 w-3" /> back
          </button>
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight font-mono break-all">
            {data.session_id}
          </h1>
          <div className="flex items-center gap-2">
            {latest && <VerdictBadge verdict={latest.operational_status} />}
            <span className="text-xs text-muted-foreground">{runs.length} QA run(s)</span>
            {data.checkpoint_hint && (
              <span className="text-[10px] rounded border px-1.5 py-0.5 uppercase text-muted-foreground">
                {data.checkpoint_hint}
              </span>
            )}
          </div>
        </div>
        <div className="flex gap-2 flex-wrap">
          <Button
            asChild
            variant="secondary"
            data-testid="session-detail-download-json"
          >
            <a href={`${BACKEND}/api/reports/export?fmt=json&session_id=${encodeURIComponent(sessionId)}`}>
              <Download className="h-4 w-4 mr-1" /> JSON
            </a>
          </Button>
          <Button asChild variant="secondary" data-testid="session-detail-download-csv">
            <a href={`${BACKEND}/api/reports/export?fmt=csv&session_id=${encodeURIComponent(sessionId)}`}>
              <Download className="h-4 w-4 mr-1" /> CSV
            </a>
          </Button>
          <Button asChild variant="secondary" data-testid="session-detail-download-md">
            <a href={`${BACKEND}/api/reports/export?fmt=md&session_id=${encodeURIComponent(sessionId)}`}>
              <Download className="h-4 w-4 mr-1" /> Markdown
            </a>
          </Button>
          <Button
            onClick={reprocess}
            disabled={busy || !latest?.retained}
            data-testid="session-detail-reprocess-button"
            title={latest?.retained ? "Reprocess retained raw ZIP" : "No retained raw ZIP"}
          >
            <RefreshCcw className="h-4 w-4 mr-2" /> Reprocess
          </Button>
        </div>
      </div>

      {latest && (
        <Card data-testid="session-detail-section">
          <CardHeader>
            <CardTitle className="text-sm font-semibold tracking-wide">Latest QA run</CardTitle>
          </CardHeader>
          <CardContent>
            <Accordion type="multiple" defaultValue={["identity", "verdict", "runtime"]}>
              <AccordionItem value="identity">
                <AccordionTrigger>Identity</AccordionTrigger>
                <AccordionContent>
                  <KV label="session_id" mono value={latest.session_id} />
                  <KV label="original_filename" value={latest.original_filename} />
                  <KV label="uploaded_at" mono value={latest.uploaded_at} />
                  <KV label="source_file_sha256" mono value={latest.source_file_sha256} />
                  <KV label="collector_sha256" mono value={latest.collector_sha256} />
                  <KV label="start_time" mono value={latest.start_time} />
                  <KV label="end_time" mono value={latest.end_time} />
                  <KV label="duration_hours" value={latest.duration_hours} />
                  <KV label="duplicate_status" value={latest.duplicate_status} />
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="verdict">
                <AccordionTrigger>Verdict &amp; reasons</AccordionTrigger>
                <AccordionContent>
                  <KV label="operational_status" value={<VerdictBadge verdict={latest.operational_status} size="sm" />} />
                  <KV label="validated_hours" mono value={latest.validated_hours?.toFixed?.(2) ?? latest.validated_hours} />
                  <KV
                    label="failure_reasons"
                    value={
                      (latest.failure_reasons || []).length ? (
                        <ul className="list-disc pl-5 text-xs">
                          {latest.failure_reasons.map((r, i) => <li key={i}>{r}</li>)}
                        </ul>
                      ) : "—"
                    }
                  />
                  <KV
                    label="warnings"
                    value={
                      (latest.warnings || []).length ? (
                        <ul className="list-disc pl-5 text-xs">
                          {latest.warnings.map((r, i) => <li key={i}>{r}</li>)}
                        </ul>
                      ) : "—"
                    }
                  />
                  <KV
                    label="missing_fields"
                    value={
                      (latest.missing_fields || []).length ? (
                        <ul className="list-disc pl-5 text-xs font-mono">
                          {latest.missing_fields.map((r, i) => <li key={i}>{r}</li>)}
                        </ul>
                      ) : "—"
                    }
                  />
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="runtime">
                <AccordionTrigger>Runtime</AccordionTrigger>
                <AccordionContent>
                  <KV label="zip_crc_status" value={latest.zip_crc_status} />
                  <KV label="manifest_status" value={latest.manifest_status} />
                  <KV label="runtime_status" value={latest.runtime_status} />
                  <KV label="watchdog_status" value={latest.watchdog_status} />
                  <KV label="exit_code" value={latest.exit_code} />
                  <KV label="writer_errors" value={latest.writer_errors} />
                  <KV label="websocket_errors" value={latest.websocket_errors} />
                  <KV label="unknown_side_count" value={latest.unknown_side_count} />
                  <KV label="missed_ticks" value={latest.missed_ticks} />
                  <KV label="theoretical_ticks" value={latest.theoretical_ticks} />
                  <KV label="missed_tick_pct" value={latest.missed_tick_pct} />
                  <KV label="lag_gt_50ms" value={latest.lag_gt_50ms} />
                  <KV label="max_lag_ms" value={latest.max_lag_ms} />
                  <KV label="final_buffer_status" value={latest.final_buffer_status} />
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="dataset">
                <AccordionTrigger>Dataset structure</AccordionTrigger>
                <AccordionContent>
                  <KV label="sync_grid_file_count" value={latest.sync_grid_file_count} />
                  <KV label="books_file_count" value={latest.books_file_count} />
                  <KV label="trades_file_count" value={latest.trades_file_count} />
                  <KV label="parquet_total" value={latest.parquet_total} />
                  <KV label="parquet_magic_status" value={latest.parquet_magic_status} />
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="reconnects">
                <AccordionTrigger>Reconnects</AccordionTrigger>
                <AccordionContent>
                  <KV label="reconnect_count" value={latest.reconnect_count} />
                  <KV
                    label="reconnect_summary"
                    mono
                    value={<pre className="whitespace-pre-wrap text-[10px]">{JSON.stringify(latest.reconnect_summary, null, 2)}</pre>}
                  />
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="retention">
                <AccordionTrigger>Retention</AccordionTrigger>
                <AccordionContent>
                  <KV label="retained" value={latest.retained ? "YES" : "NO"} />
                  <KV label="retention_reason" value={latest.retention_reason} />
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="checks">
                <AccordionTrigger>Per-check breakdown</AccordionTrigger>
                <AccordionContent>
                  <pre className="font-mono text-[10px] whitespace-pre-wrap">
                    {JSON.stringify(latest.checks, null, 2)}
                  </pre>
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="manifest">
                <AccordionTrigger>Manifest raw</AccordionTrigger>
                <AccordionContent>
                  <pre className="font-mono text-[10px] whitespace-pre-wrap">
                    {JSON.stringify(latest.manifest_raw, null, 2) || "—"}
                  </pre>
                </AccordionContent>
              </AccordionItem>
            </Accordion>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-semibold tracking-wide">Reprocess history</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="divide-y">
            {runs.map((r) => (
              <li key={r.id} className="flex items-center justify-between py-2">
                <div className="flex items-center gap-3">
                  <VerdictBadge verdict={r.operational_status} size="sm" />
                  <span className="font-mono text-[10px] text-muted-foreground">{r.uploaded_at}</span>
                </div>
                <div className="text-[10px] text-muted-foreground font-mono">{r.source_file_sha256.slice(0, 16)}…</div>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
