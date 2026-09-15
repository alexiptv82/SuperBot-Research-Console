import React, { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "@/lib/api";
import { useT, useLocale } from "@/lib/locale";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { VerdictBadge, DuplicateStateText } from "@/components/VerdictBadge";
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
  const t = useT();
  const { fmtDate, fmtNumber } = useLocale();
  const { sessionId } = useParams();
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();

  const load = () => api.get(`/sessions/${encodeURIComponent(sessionId)}`).then((r) => setData(r.data));
  useEffect(() => { load(); }, [sessionId]);

  if (!data) return <div className="text-sm text-muted-foreground">{t("common.loading")}</div>;
  const runs = data.qa_runs || [];
  const latest = runs[0];

  const reprocess = async () => {
    setBusy(true);
    try {
      await api.post(`/sessions/${encodeURIComponent(sessionId)}/reprocess`);
      await load();
    } catch (e) {
      alert(e?.response?.data?.detail || t("detail.reprocess_failed"));
    } finally { setBusy(false); }
  };

  return (
    <div className="space-y-6" data-testid="session-detail-header">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="space-y-1">
          <button onClick={() => nav(-1)} className="text-xs text-muted-foreground hover:underline flex items-center gap-1">
            <ArrowLeft className="h-3 w-3" /> {t("detail.back")}
          </button>
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight font-mono break-all">{data.session_id}</h1>
          <div className="flex items-center gap-2">
            {latest && <VerdictBadge verdict={latest.operational_status} />}
            <span className="text-xs text-muted-foreground">{t("detail.runs_count", { n: runs.length })}</span>
            {data.checkpoint_hint && (
              <span className="text-[10px] rounded border px-1.5 py-0.5 uppercase text-muted-foreground">{data.checkpoint_hint}</span>
            )}
          </div>
        </div>
        <div className="flex gap-2 flex-wrap">
          <Button asChild variant="secondary" data-testid="session-detail-download-json">
            <a href={`${BACKEND}/api/reports/export?fmt=json&session_id=${encodeURIComponent(sessionId)}`}>
              <Download className="h-4 w-4 mr-1" /> {t("detail.download_json")}
            </a>
          </Button>
          <Button asChild variant="secondary" data-testid="session-detail-download-csv">
            <a href={`${BACKEND}/api/reports/export?fmt=csv&session_id=${encodeURIComponent(sessionId)}`}>
              <Download className="h-4 w-4 mr-1" /> {t("detail.download_csv")}
            </a>
          </Button>
          <Button asChild variant="secondary" data-testid="session-detail-download-md">
            <a href={`${BACKEND}/api/reports/export?fmt=md&session_id=${encodeURIComponent(sessionId)}`}>
              <Download className="h-4 w-4 mr-1" /> {t("detail.download_md")}
            </a>
          </Button>
          <Button onClick={reprocess} disabled={busy || !latest?.retained} data-testid="session-detail-reprocess-button"
            title={latest?.retained ? t("detail.reprocess_tooltip_ok") : t("detail.reprocess_tooltip_ko")}>
            <RefreshCcw className="h-4 w-4 mr-2" /> {t("detail.reprocess")}
          </Button>
        </div>
      </div>

      {latest && (
        <Card data-testid="session-detail-section">
          <CardHeader>
            <CardTitle className="text-sm font-semibold tracking-wide">{t("detail.latest")}</CardTitle>
          </CardHeader>
          <CardContent>
            <Accordion type="multiple" defaultValue={["identity", "verdict", "runtime"]}>
              <AccordionItem value="identity">
                <AccordionTrigger>{t("detail.section.identity")}</AccordionTrigger>
                <AccordionContent>
                  <KV label="session_id" mono value={latest.session_id} />
                  <KV label="original_filename" value={latest.original_filename} />
                  <KV label="uploaded_at" mono value={fmtDate(latest.uploaded_at)} />
                  <KV label="source_file_sha256" mono value={latest.source_file_sha256} />
                  <KV label="collector_sha256" mono value={latest.collector_sha256} />
                  <KV label="start_time" mono value={latest.start_time} />
                  <KV label="end_time" mono value={latest.end_time} />
                  <KV label="duration_hours" value={latest.duration_hours} />
                  <KV label="duplicate_status" value={<DuplicateStateText state={latest.duplicate_status} />} />
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="verdict">
                <AccordionTrigger>{t("detail.section.verdict")}</AccordionTrigger>
                <AccordionContent>
                  <KV label="operational_status" value={<VerdictBadge verdict={latest.operational_status} size="sm" />} />
                  <KV label="validated_hours" mono value={fmtNumber(latest.validated_hours || 0, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} />
                  <KV label={t("detail.field.failure_reasons")}
                    value={(latest.failure_reasons || []).length ? (<ul className="list-disc pl-5 text-xs">{latest.failure_reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>) : t("common.none")} />
                  <KV label={t("detail.field.warnings")}
                    value={(latest.warnings || []).length ? (<ul className="list-disc pl-5 text-xs">{latest.warnings.map((r, i) => <li key={i}>{r}</li>)}</ul>) : t("common.none")} />
                  <KV label={t("detail.field.missing_fields")}
                    value={(latest.missing_fields || []).length ? (<ul className="list-disc pl-5 text-xs font-mono">{latest.missing_fields.map((r, i) => <li key={i}>{r}</li>)}</ul>) : t("common.none")} />
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="runtime">
                <AccordionTrigger>{t("detail.section.runtime")}</AccordionTrigger>
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
                <AccordionTrigger>{t("detail.section.dataset")}</AccordionTrigger>
                <AccordionContent>
                  <KV label="sync_grid_file_count" value={latest.sync_grid_file_count} />
                  <KV label="books_file_count" value={latest.books_file_count} />
                  <KV label="trades_file_count" value={latest.trades_file_count} />
                  <KV label="parquet_total" value={latest.parquet_total} />
                  <KV label="parquet_magic_status" value={latest.parquet_magic_status} />
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="reconnects">
                <AccordionTrigger>{t("detail.section.reconnects")}</AccordionTrigger>
                <AccordionContent>
                  <KV label="reconnect_count" value={latest.reconnect_count} />
                  <KV label="reconnect_summary" mono value={<pre className="whitespace-pre-wrap text-[10px]">{JSON.stringify(latest.reconnect_summary, null, 2)}</pre>} />
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="retention">
                <AccordionTrigger>{t("detail.section.retention")}</AccordionTrigger>
                <AccordionContent>
                  <KV label={t("detail.field.retained")} value={latest.retained ? t("detail.retained_yes") : t("detail.retained_no")} />
                  <KV label={t("detail.field.retention_reason")} value={latest.retention_reason} />
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="checks">
                <AccordionTrigger>{t("detail.section.checks")}</AccordionTrigger>
                <AccordionContent>
                  <pre className="font-mono text-[10px] whitespace-pre-wrap">{JSON.stringify(latest.checks, null, 2)}</pre>
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="manifest">
                <AccordionTrigger>{t("detail.section.manifest")}</AccordionTrigger>
                <AccordionContent>
                  <pre className="font-mono text-[10px] whitespace-pre-wrap">{JSON.stringify(latest.manifest_raw, null, 2) || t("common.none")}</pre>
                </AccordionContent>
              </AccordionItem>
            </Accordion>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-semibold tracking-wide">{t("detail.reprocess_history")}</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="divide-y">
            {runs.map((r) => (
              <li key={r.id} className="flex items-center justify-between py-2">
                <div className="flex items-center gap-3">
                  <VerdictBadge verdict={r.operational_status} size="sm" />
                  <span className="font-mono text-[10px] text-muted-foreground">{fmtDate(r.uploaded_at)}</span>
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
