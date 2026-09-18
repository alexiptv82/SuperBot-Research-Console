import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { uploadChunked } from "@/lib/chunkedUpload";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { AlertTriangle, CheckCircle2, Loader2, Package, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  OLD36_CHECKPOINT_HINT,
  OLD36_RETAIN_RAW,
  OUTCOME,
  deriveOld36MissingState,
  matchFileToMissingSession,
  runSequentialOld36Import,
} from "@/lib/old36MissingRaw";

function fmtBytes(n) {
  if (!Number.isFinite(n)) return "\u2014";
  const units = ["B", "KB", "MB", "GB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 2)} ${units[i]}`;
}

function MetricTile({ label, value, testid }) {
  return (
    <div className="rounded-lg border bg-background/40 px-3 py-2" data-testid={testid}>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="mt-1 text-sm font-semibold font-mono">{value}</div>
    </div>
  );
}

const OUTCOME_STYLE = {
  [OUTCOME.RETAINED]: "text-[hsl(var(--verdict-pass))]",
  [OUTCOME.EXACT_DUPLICATE]: "text-muted-foreground",
  [OUTCOME.BLOCKED_DISK_SPACE]: "text-[hsl(var(--verdict-fail))]",
  [OUTCOME.OLD36_IDENTITY_REJECTED]: "text-[hsl(var(--verdict-fail))]",
  [OUTCOME.QA_FAILED]: "text-[hsl(var(--verdict-fail))]",
  [OUTCOME.GENERIC_FAILURE]: "text-[hsl(var(--verdict-fail))]",
};

function OutcomeBadge({ outcome, t }) {
  const cls = OUTCOME_STYLE[outcome] || "text-muted-foreground";
  const label = t(`old36missing.outcome.${outcome}`);
  return <span className={cn("uppercase tracking-wider", cls)}>{label}</span>;
}

export default function Old36MissingRawPanel({ t, fmtNumber }) {
  const [refState, setRefState] = useState(null);
  const [loadingState, setLoadingState] = useState(true);
  const [files, setFiles] = useState([]);
  const [running, setRunning] = useState(false);
  const [currentIndex, setCurrentIndex] = useState(-1);
  const [currentProgress, setCurrentProgress] = useState({ uploaded: 0, total: 0 });
  const [rowResults, setRowResults] = useState([]);
  const [stopped, setStopped] = useState(null);
  const [finalSummary, setFinalSummary] = useState(null);
  const inputRef = useRef(null);
  const abortRef = useRef(null);
  const [drag, setDrag] = useState(false);

  const fetchState = useCallback(async () => {
    const r = await api.get("/reference/old36");
    setRefState(r.data);
    return r.data;
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoadingState(true);
    fetchState().finally(() => {
      if (!cancelled) setLoadingState(false);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const derived = useMemo(() => deriveOld36MissingState(refState), [refState]);
  const missingIds = derived.missingSessionIds;

  const onSelect = (list) => {
    const arr = Array.from(list || []).filter((f) => /\.zip$/i.test(f.name));
    setFiles(arr);
    setRowResults([]);
    setStopped(null);
    setFinalSummary(null);
  };

  const onDrop = (e) => {
    e.preventDefault();
    setDrag(false);
    if (e.dataTransfer?.files?.length) onSelect(e.dataTransfer.files);
  };

  const start = async () => {
    if (!files.length || running) return;
    setRunning(true);
    setStopped(null);
    setFinalSummary(null);
    setRowResults([]);
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    const uploadOne = (file) =>
      uploadChunked({
        file,
        retainRaw: OLD36_RETAIN_RAW,
        checkpointHint: OLD36_CHECKPOINT_HINT,
        signal: ctrl.signal,
        onProgress: (p) => {
          if (p.totalBytes != null) {
            setCurrentProgress({ uploaded: p.uploadedBytes || 0, total: p.totalBytes });
          }
        },
      });

    const outcome = await runSequentialOld36Import({
      files,
      uploadOne,
      fetchState,
      onEvent: (ev) => {
        if (ev.type === "file_start") {
          setCurrentIndex(ev.index);
          setCurrentProgress({ uploaded: 0, total: files[ev.index]?.size || 0 });
        } else if (ev.type === "file_result") {
          setRowResults((prev) => [...prev, ev]);
        } else if (ev.type === "sequence_stopped") {
          setStopped(ev);
        }
      },
    });

    setFinalSummary(outcome);
    setRunning(false);
    setCurrentIndex(-1);
    abortRef.current = null;
  };

  const pct = currentProgress.total
    ? Math.min(100, (currentProgress.uploaded / currentProgress.total) * 100)
    : 0;
  const currentFile = currentIndex >= 0 ? files[currentIndex] : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-semibold tracking-wide">
          {t("old36missing.title")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-xs text-muted-foreground">{t("old36missing.subtitle")}</p>
        <p className="text-[11px] text-muted-foreground italic">{t("bundle.baseline_note")}</p>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs" data-testid="old36-missing-state">
          <MetricTile
            label={t("old36missing.present")}
            value={loadingState ? "\u2026" : `${derived.presentSessions} / ${derived.expectedSessions}`}
            testid="old36-missing-metric-present"
          />
          <MetricTile
            label={t("old36missing.raw_retained")}
            value={loadingState ? "\u2026" : `${derived.rawRetainedSessions} / ${derived.expectedSessions}`}
            testid="old36-missing-metric-retained"
          />
          <MetricTile
            label={t("old36missing.missing_count")}
            value={loadingState ? "\u2026" : missingIds.length}
            testid="old36-missing-metric-missing-count"
          />
          <MetricTile
            label={t("old36missing.raw_ready")}
            value={loadingState ? "\u2026" : derived.rawReady ? t("old36missing.ready_true") : t("old36missing.ready_false")}
            testid="old36-missing-metric-ready"
          />
        </div>

        {!loadingState && missingIds.length > 0 && (
          <div className="rounded-lg border bg-background/40 p-3" data-testid="old36-missing-session-list">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1">
              {t("old36missing.missing_ids_title")}
            </div>
            <ul className="text-[11px] font-mono space-y-0.5">
              {missingIds.map((sid) => (
                <li key={sid} data-testid={`old36-missing-id-${sid}`}>{sid}</li>
              ))}
            </ul>
          </div>
        )}

        <div
          data-testid="old36-missing-dropzone"
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={onDrop}
          onClick={() => inputRef.current?.click()}
          className={cn(
            "rounded-xl border border-dashed bg-card p-6 transition-colors cursor-pointer flex flex-col items-center justify-center text-center",
            drag ? "border-[hsl(var(--focus))] bg-[hsl(var(--focus)/0.06)]" : "border-border hover:border-[hsl(var(--focus))]",
          )}
        >
          <Package className="h-8 w-8 text-muted-foreground" />
          <div className="mt-3 text-sm font-medium">{t("old36missing.drop_hint")}</div>
          <div className="mt-1 text-xs text-muted-foreground">{t("old36missing.drop_note")}</div>
          <input
            ref={inputRef}
            data-testid="old36-missing-file-input"
            type="file"
            multiple
            accept=".zip"
            className="hidden"
            onChange={(e) => onSelect(e.target.files)}
          />
        </div>

        {files.length > 0 && (
          <div className="overflow-x-auto rounded-lg border" data-testid="old36-missing-file-table">
            <table className="min-w-full text-[11px]">
              <thead className="bg-muted/40 text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">#</th>
                  <th className="px-3 py-2 text-left font-medium">{t("old36missing.filename")}</th>
                  <th className="px-3 py-2 text-left font-medium">{t("old36missing.matched_session")}</th>
                  <th className="px-3 py-2 text-left font-medium">{t("old36missing.status")}</th>
                </tr>
              </thead>
              <tbody>
                {files.map((f, i) => {
                  const matched = matchFileToMissingSession(f.name, missingIds);
                  const rowResult = rowResults.find((r) => r.index === i);
                  return (
                    <tr key={`${f.name}:${i}`} className="border-t" data-testid={`old36-missing-file-row-${i}`}>
                      <td className="px-3 py-2 font-mono">{i}</td>
                      <td className="px-3 py-2 font-mono truncate max-w-[220px]">{f.name}</td>
                      <td className="px-3 py-2 font-mono">
                        {matched || (
                          <span className="text-[hsl(var(--verdict-warn))]">
                            {t("old36missing.unmatched")}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2">
                        {i === currentIndex ? (
                          <span className="uppercase tracking-wider text-foreground">
                            {t("old36missing.uploading_current")}
                          </span>
                        ) : rowResult ? (
                          <OutcomeBadge outcome={rowResult.outcome} t={t} />
                        ) : (
                          <span className="text-muted-foreground">{t("old36missing.queued")}</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <div className="flex items-center gap-3">
          <Button
            data-testid="old36-missing-start-button"
            onClick={start}
            disabled={running || files.length === 0}
          >
            {running && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
            {t("old36missing.start")}
          </Button>
          {files.length > 0 && (
            <span className="text-xs text-muted-foreground" data-testid="old36-missing-selected-count">
              {t("old36missing.files_selected", { n: files.length })}
            </span>
          )}
        </div>

        {running && currentFile && (
          <div className="space-y-2" data-testid="old36-missing-current-progress">
            <div className="flex items-center justify-between text-[10px] text-muted-foreground font-mono">
              <span className="uppercase tracking-wider text-foreground">
                {t("old36missing.current_file", { n: currentIndex + 1, total: files.length })}: {currentFile.name}
              </span>
              <span>
                {fmtBytes(currentProgress.uploaded)} / {fmtBytes(currentProgress.total)} · {fmtNumber(pct, { maximumFractionDigits: 1 })}%
              </span>
            </div>
            <Progress value={pct} />
          </div>
        )}

        {stopped && (
          <div
            data-testid="old36-missing-stopped"
            className="rounded-md border border-[hsl(var(--verdict-fail)/0.4)] bg-[hsl(var(--verdict-fail)/0.06)] px-3 py-2 text-xs text-[hsl(var(--verdict-fail))] flex items-center gap-2"
          >
            <AlertTriangle className="h-4 w-4" />
            {t("old36missing.stopped", { outcome: t(`old36missing.outcome.${stopped.outcome}`) })}
          </div>
        )}

        {finalSummary && (
          <div className="rounded-lg border bg-background/40 p-3 space-y-1" data-testid="old36-missing-final-summary">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              {t("old36missing.final_summary_title")}
            </div>
            <div className="text-xs font-mono">
              {t("old36missing.final_summary_line", {
                ok: finalSummary.results.filter(
                  (r) => r.outcome === OUTCOME.RETAINED || r.outcome === OUTCOME.EXACT_DUPLICATE,
                ).length,
                total: finalSummary.results.length,
              })}
            </div>
            <div className="text-xs">
              {derived.rawReady ? (
                <span className="text-[hsl(var(--verdict-pass))] flex items-center gap-1">
                  <CheckCircle2 className="h-3.5 w-3.5" /> {t("old36missing.ready_true")}
                </span>
              ) : (
                <span className="text-[hsl(var(--verdict-warn))] flex items-center gap-1">
                  <XCircle className="h-3.5 w-3.5" /> {t("old36missing.ready_false")}
                </span>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
