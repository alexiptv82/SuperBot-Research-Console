import React, { useCallback, useMemo, useRef, useState } from "react";
import { useT, useLocale } from "@/lib/locale";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { VerdictBadge, DuplicateStateText } from "@/components/VerdictBadge";
import { Upload as UploadIcon, X, FileArchive, Loader2, Package } from "lucide-react";
import { cn } from "@/lib/utils";
import { Link } from "react-router-dom";
import { uploadChunked } from "@/lib/chunkedUpload";
import { uploadBundleChunked } from "@/lib/chunkedBundleUpload";

const CHECKPOINTS = ["", "OLD36", "NEW12", "NEW36"];
const STATES = {
  QUEUED: "QUEUED",
  PREPARING: "PREPARING",
  UPLOADING: "UPLOADING",
  ASSEMBLING: "ASSEMBLING",
  UPLOADED: "UPLOADED",
  QA: "QA",
  DONE: "DONE",
  ERROR: "ERROR",
};

function fmtBytes(n) {
  if (!Number.isFinite(n)) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 2)} ${units[i]}`;
}

export default function UploadPage() {
  const t = useT();
  const { fmtNumber } = useLocale();
  const [mode, setMode] = useState("single");
  const [files, setFiles] = useState([]);
  const [drag, setDrag] = useState(false);
  const [retainRaw, setRetainRaw] = useState(false);
  const [checkpoint, setCheckpoint] = useState("");
  const [busy, setBusy] = useState(false);
  const inputRef = useRef(null);
  const abortRefs = useRef({});

  const addFiles = useCallback((selected) => {
    const arr = Array.from(selected || []);
    setFiles((prev) => [
      ...prev,
      ...arr.map((f) => ({
        file: f,
        id: `${f.name}:${f.size}:${Math.random().toString(36).slice(2, 8)}`,
        state: STATES.QUEUED,
        uploadedBytes: 0,
        totalBytes: f.size,
        result: null,
        error: null,
        retryAttempt: 0,
      })),
    ]);
  }, []);

  const onDrop = (e) => {
    e.preventDefault();
    setDrag(false);
    if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files);
  };

  const remove = (id) => {
    const ctrl = abortRefs.current[id];
    if (ctrl && !ctrl.signal.aborted) ctrl.abort();
    setFiles((prev) => prev.filter((f) => f.id !== id));
  };

  const updateOne = useCallback((id, patch) => {
    setFiles((prev) => prev.map((f) => (f.id === id ? { ...f, ...patch } : f)));
  }, []);

  const upload = async () => {
    if (!files.length) return;
    setBusy(true);
    for (const item of files) {
      if (item.state === STATES.DONE) continue;
      const ctrl = new AbortController();
      abortRefs.current[item.id] = ctrl;
      updateOne(item.id, { state: STATES.PREPARING, error: null, uploadedBytes: 0 });
      try {
        const result = await uploadChunked({
          file: item.file,
          retainRaw,
          checkpointHint: checkpoint,
          signal: ctrl.signal,
          onProgress: (p) => {
            if (p.phase === "prepare" || p.phase === "hashing") {
              updateOne(item.id, { state: STATES.PREPARING });
            } else if (p.phase === "retry") {
              updateOne(item.id, { state: STATES.UPLOADING, retryAttempt: p.attempt });
            } else if (p.phase === "uploading") {
              updateOne(item.id, {
                state: STATES.UPLOADING,
                uploadedBytes: p.uploadedBytes,
                totalBytes: p.totalBytes,
                retryAttempt: 0,
              });
            } else if (p.phase === "assembling") {
              updateOne(item.id, {
                state: STATES.ASSEMBLING,
                uploadedBytes: p.uploadedBytes,
              });
            } else if (p.phase === "qa") {
              updateOne(item.id, { state: STATES.QA });
            }
          },
        });
        updateOne(item.id, { state: STATES.DONE, result, uploadedBytes: item.totalBytes });
      } catch (e) {
        updateOne(item.id, {
          state: STATES.ERROR,
          error: e?.message || t("upload.err.unknown"),
        });
      } finally {
        delete abortRefs.current[item.id];
      }
    }
    setBusy(false);
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight">{t("upload.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("upload.subtitle")}</p>
      </div>

      <div className="inline-flex rounded-lg border bg-card p-1 gap-1" data-testid="upload-mode-tabs">
        <button
          type="button"
          data-testid="upload-mode-single"
          onClick={() => setMode("single")}
          className={cn(
            "px-3 py-1.5 text-xs font-medium rounded-md transition-colors",
            mode === "single"
              ? "bg-[hsl(var(--focus))] text-white"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <FileArchive className="inline h-3.5 w-3.5 mr-1.5 -mt-0.5" />
          {t("upload.mode.single")}
        </button>
        <button
          type="button"
          data-testid="upload-mode-bundle"
          onClick={() => setMode("bundle")}
          className={cn(
            "px-3 py-1.5 text-xs font-medium rounded-md transition-colors",
            mode === "bundle"
              ? "bg-[hsl(var(--focus))] text-white"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <Package className="inline h-3.5 w-3.5 mr-1.5 -mt-0.5" />
          {t("upload.mode.bundle")}
        </button>
      </div>

      {mode === "bundle" && <BundlePanel t={t} fmtNumber={fmtNumber} />}

      {mode === "single" && (
      <>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-semibold tracking-wide">{t("upload.ingest")}</CardTitle>
        </CardHeader>
        <CardContent>
          <div
            data-testid="upload-dropzone"
            onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
            onDragLeave={() => setDrag(false)}
            onDrop={onDrop}
            onClick={() => inputRef.current?.click()}
            className={cn(
              "rounded-xl border border-dashed bg-card p-6 sm:p-10 transition-colors cursor-pointer flex flex-col items-center justify-center text-center",
              drag
                ? "border-[hsl(var(--focus))] bg-[hsl(var(--focus)/0.06)]"
                : "border-border hover:border-[hsl(var(--focus))]",
            )}
          >
            <UploadIcon className="h-8 w-8 text-muted-foreground" />
            <div className="mt-3 text-sm font-medium">{t("upload.drop_hint")}</div>
            <div className="mt-1 text-xs text-muted-foreground">{t("upload.drop_note")}</div>
            <input
              ref={inputRef}
              data-testid="upload-file-input"
              type="file"
              multiple
              accept=".zip"
              className="hidden"
              onChange={(e) => addFiles(e.target.files)}
            />
          </div>

          <div className="mt-4 flex flex-col sm:flex-row sm:items-center gap-3 justify-between">
            <div className="flex items-center gap-3">
              <Switch id="retain" checked={retainRaw} onCheckedChange={setRetainRaw} data-testid="upload-retain-raw-toggle" />
              <Label htmlFor="retain" className="text-xs">{t("upload.retain")}</Label>
            </div>
            <div className="flex items-center gap-2 text-xs">
              <span className="text-muted-foreground">{t("upload.checkpoint_hint")}</span>
              <select
                data-testid="upload-checkpoint-hint"
                className="rounded-md border bg-background px-2 py-1 text-xs"
                value={checkpoint}
                onChange={(e) => setCheckpoint(e.target.value)}
              >
                {CHECKPOINTS.map((c) => (
                  <option key={c} value={c}>{c || t("upload.checkpoint_auto")}</option>
                ))}
              </select>
            </div>
            <Button onClick={upload} disabled={busy || files.length === 0} data-testid="upload-start-button">
              {busy && <Loader2 className="h-4 w-4 mr-2 animate-spin" />} {t("upload.start")} {files.length ? `(${files.length})` : ""}
            </Button>
          </div>
        </CardContent>
      </Card>

      {files.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-semibold tracking-wide">{t("upload.queue")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {files.map((f) => (
              <FileRow
                key={f.id}
                item={f}
                onRemove={() => remove(f.id)}
                t={t}
                fmtNumber={fmtNumber}
              />
            ))}
          </CardContent>
        </Card>
      )}
      </>
      )}
    </div>
  );
}

function FileRow({ item, onRemove, t, fmtNumber }) {
  const pct = item.totalBytes ? Math.min(100, (item.uploadedBytes / item.totalBytes) * 100) : 0;
  const stateKey = `upload.state.${item.state}`;
  const stateLabel = t(stateKey);
  return (
    <div data-testid="upload-file-row" data-state={item.state} className="rounded-lg border bg-background/40 px-3 py-3">
      <div className="flex items-center gap-3">
        <FileArchive className="h-4 w-4 text-muted-foreground" />
        <div className="flex-1 min-w-0">
          <div className="text-sm truncate">{item.file.name}</div>
          <div className="text-[10px] text-muted-foreground font-mono">
            {fmtBytes(item.totalBytes)}
            {item.result?.file_sha256 && (
              <span className="ml-2">sha256={item.result.file_sha256.slice(0, 12)}…</span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {item.state === STATES.DONE && item.result && (
            <>
              <span className="text-[10px] text-muted-foreground uppercase">
                <DuplicateStateText state={item.result.duplicate_status} />
              </span>
              <VerdictBadge verdict={item.result.verdict} size="sm" />
              {item.result.session_id && (
                <Link
                  to={`/session/${encodeURIComponent(item.result.session_id)}`}
                  className="text-xs text-[hsl(var(--focus))] hover:underline"
                  data-testid="upload-file-view"
                >
                  {t("upload.file_view")}
                </Link>
              )}
            </>
          )}
          <Button variant="ghost" size="icon" onClick={onRemove} aria-label={t("upload.remove")} data-testid="upload-file-remove-button">
            <X className="h-4 w-4" />
          </Button>
        </div>
      </div>
      {item.state !== STATES.QUEUED && item.state !== STATES.DONE && (
        <div className="mt-2 space-y-1">
          <div className="flex items-center justify-between text-[10px] text-muted-foreground font-mono">
            <span
              data-testid="upload-status-label"
              className="uppercase tracking-wider text-foreground"
            >
              {stateLabel}
              {item.retryAttempt > 0 && (
                <span className="ml-2 text-[hsl(var(--verdict-warn))]">
                  {t("upload.retry", { n: item.retryAttempt })}
                </span>
              )}
            </span>
            <span>
              {fmtBytes(item.uploadedBytes)} / {fmtBytes(item.totalBytes)} · {fmtNumber(pct, { maximumFractionDigits: 1 })}%
            </span>
          </div>
          <Progress value={pct} />
        </div>
      )}
      {item.state === STATES.ERROR && (
        <div
          data-testid="upload-file-error"
          className="mt-2 text-xs text-[hsl(var(--verdict-fail))] break-words"
        >
          {t("upload.state.ERROR")}: {item.error}
        </div>
      )}
    </div>
  );
}

function BundlePanel({ t, fmtNumber }) {
  const [subMode, setSubMode] = React.useState("single");
  return (
    <div className="space-y-3">
      <div className="inline-flex rounded-lg border bg-card p-1 gap-1" data-testid="bundle-submode-tabs">
        <button
          type="button"
          data-testid="bundle-submode-single"
          onClick={() => setSubMode("single")}
          className={cn(
            "px-3 py-1 text-[11px] font-medium rounded-md transition-colors",
            subMode === "single"
              ? "bg-[hsl(var(--focus))] text-white"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {t("bundle.submode.single")}
        </button>
        <button
          type="button"
          data-testid="bundle-submode-multipart"
          onClick={() => setSubMode("multipart")}
          className={cn(
            "px-3 py-1 text-[11px] font-medium rounded-md transition-colors",
            subMode === "multipart"
              ? "bg-[hsl(var(--focus))] text-white"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {t("bundle.submode.multipart")}
        </button>
      </div>
      {subMode === "single" ? (
        <SingleBundlePanel t={t} fmtNumber={fmtNumber} />
      ) : (
        <MultipartBundlePanel t={t} fmtNumber={fmtNumber} />
      )}
    </div>
  );
}

function SingleBundlePanel({ t, fmtNumber }) {
  const [file, setFile] = React.useState(null);
  const [state, setState] = React.useState("IDLE");
  const [uploadedBytes, setUploadedBytes] = React.useState(0);
  const [totalBytes, setTotalBytes] = React.useState(0);
  const [error, setError] = React.useState(null);
  const [result, setResult] = React.useState(null);
  const inputRef = React.useRef(null);
  const [drag, setDrag] = React.useState(false);

  const onDrop = (e) => {
    e.preventDefault();
    setDrag(false);
    const f = e.dataTransfer?.files?.[0];
    if (f) { setFile(f); setResult(null); setError(null); setState("IDLE"); }
  };

  const start = async () => {
    if (!file) return;
    setState("PREPARING");
    setError(null);
    setResult(null);
    setUploadedBytes(0);
    setTotalBytes(file.size);
    try {
      const res = await uploadBundleChunked({
        file,
        onProgress: (p) => {
          if (p.totalBytes) setTotalBytes(p.totalBytes);
          if (p.uploadedBytes != null) setUploadedBytes(p.uploadedBytes);
          if (p.phase === "prepare" || p.phase === "hashing") setState("PREPARING");
          else if (p.phase === "uploading" || p.phase === "retry") setState("UPLOADING");
          else if (p.phase === "assembling") setState("VERIFYING");
          else if (p.phase === "verifying") setState("VERIFYING");
          else if (p.phase === "done") setState("DONE");
        },
      });
      setResult(res);
      setState("DONE");
    } catch (e) {
      setError(e?.response?.data?.detail || e?.message || "unknown error");
      setState("ERROR");
    }
  };

  const pct = totalBytes ? Math.min(100, (uploadedBytes / totalBytes) * 100) : 0;
  const activeStage = state === "PROCESSING" || state === "UPLOADING" || state === "VERIFYING" || state === "PREPARING";
  const summary = result?.results;
  const ref = result?.old36_reference;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-semibold tracking-wide">{t("bundle.title")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-xs text-muted-foreground">{t("bundle.subtitle")}</p>
        <p className="text-[11px] text-muted-foreground italic">{t("bundle.baseline_note")}</p>

        <div
          data-testid="bundle-dropzone"
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={onDrop}
          onClick={() => inputRef.current?.click()}
          className={cn(
            "rounded-xl border border-dashed bg-card p-6 sm:p-10 transition-colors cursor-pointer flex flex-col items-center justify-center text-center",
            drag
              ? "border-[hsl(var(--focus))] bg-[hsl(var(--focus)/0.06)]"
              : "border-border hover:border-[hsl(var(--focus))]",
          )}
        >
          <Package className="h-8 w-8 text-muted-foreground" />
          <div className="mt-3 text-sm font-medium">{t("bundle.drop_hint")}</div>
          <div className="mt-1 text-xs text-muted-foreground">{t("bundle.drop_note")}</div>
          <input
            ref={inputRef}
            data-testid="bundle-file-input"
            type="file"
            accept=".zip"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) { setFile(f); setResult(null); setError(null); setState("IDLE"); }
            }}
          />
        </div>

        {file && (
          <div className="rounded-lg border bg-background/40 px-3 py-3 text-xs">
            <div className="flex items-center gap-2">
              <Package className="h-4 w-4 text-muted-foreground" />
              <span className="font-medium">{t("bundle.selected")}:</span>
              <span data-testid="bundle-file-name" className="truncate">{file.name}</span>
            </div>
            <div className="mt-1 text-[10px] text-muted-foreground font-mono">
              {t("bundle.size")}: {fmtBytes(file.size)}
            </div>
          </div>
        )}

        <div>
          <Button
            data-testid="bundle-start-button"
            onClick={start}
            disabled={!file || activeStage}
          >
            {activeStage && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
            {t("bundle.start")}
          </Button>
        </div>

        {state !== "IDLE" && (
          <div className="space-y-2">
            <div className="flex items-center justify-between text-[10px] text-muted-foreground font-mono">
              <span
                data-testid="bundle-status-label"
                className="uppercase tracking-wider text-foreground"
              >
                {t(`bundle.state.${state}`)}
              </span>
              <span>
                {fmtBytes(uploadedBytes)} / {fmtBytes(totalBytes)} · {fmtNumber(pct, { maximumFractionDigits: 1 })}%
              </span>
            </div>
            <Progress value={pct} />
          </div>
        )}

        {error && (
          <div
            data-testid="bundle-error"
            className="rounded-md border border-[hsl(var(--verdict-fail)/0.4)] bg-[hsl(var(--verdict-fail)/0.06)] px-3 py-2 text-xs text-[hsl(var(--verdict-fail))]"
          >
            {t("bundle.state.ERROR")}: {error}
          </div>
        )}

        {summary && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
              <MetricTile label={t("bundle.sessions_found")} value={`${summary.total} / 11`} testid="bundle-metric-found" />
              <MetricTile label={t("bundle.summary_ok")} value={summary.ok} testid="bundle-metric-ok" />
              <MetricTile label={t("bundle.summary_failed")} value={summary.failed} testid="bundle-metric-failed" />
              {ref && (
                <MetricTile
                  label={t("bundle.old36_available")}
                  value={`${ref.present_sessions} / ${ref.expected_sessions}`}
                  testid="bundle-metric-old36-available"
                />
              )}
              {ref && (
                <MetricTile
                  label={t("bundle.old36_hours")}
                  value={`${ref.present_nominal_hours} / ${ref.expected_nominal_hours} h`}
                  testid="bundle-metric-old36-hours"
                />
              )}
            </div>

            <div className="overflow-x-auto rounded-lg border">
              <table className="min-w-full text-xs" data-testid="bundle-summary-table">
                <thead className="bg-muted/40 text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">{t("bundle.table.session")}</th>
                    <th className="px-3 py-2 text-left font-medium">{t("bundle.table.verdict")}</th>
                    <th className="px-3 py-2 text-left font-medium">{t("bundle.table.duplicate")}</th>
                    <th className="px-3 py-2 text-right font-medium">{t("bundle.table.hours")}</th>
                    <th className="px-3 py-2 text-right font-medium">{t("bundle.table.detail")}</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.sessions.map((s) => (
                    <tr key={s.session_id} data-testid={`bundle-row-${s.session_id}`} className="border-t">
                      <td className="px-3 py-2 font-mono text-[11px]">{s.session_id}</td>
                      <td className="px-3 py-2">
                        {s.ok && s.result ? (
                          <VerdictBadge verdict={s.result.verdict} size="sm" />
                        ) : (
                          <span className="text-[hsl(var(--verdict-fail))] font-mono">
                            {s.error || "\u2014"}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                        {s.result?.duplicate_status ? (
                          <DuplicateStateText state={s.result.duplicate_status} />
                        ) : "\u2014"}
                      </td>
                      <td className="px-3 py-2 text-right font-mono">
                        {s.result?.validated_hours != null
                          ? fmtNumber(s.result.validated_hours, { maximumFractionDigits: 4 })
                          : "\u2014"}
                      </td>
                      <td className="px-3 py-2 text-right">
                        {s.ok && s.result?.session_id ? (
                          <Link
                            to={`/session/${encodeURIComponent(s.result.session_id)}`}
                            className="text-[hsl(var(--focus))] hover:underline"
                          >
                            {t("upload.file_view")}
                          </Link>
                        ) : "\u2014"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function MetricTile({ label, value, testid }) {
  return (
    <div className="rounded-lg border bg-background/40 px-3 py-2" data-testid={testid}>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="mt-1 text-sm font-semibold font-mono">{value}</div>
    </div>
  );
}

function MultipartBundlePanel({ t, fmtNumber }) {
  const [files, setFiles] = React.useState([]);
  const [state, setState] = React.useState("IDLE");
  const [uploaded, setUploaded] = React.useState(0);
  const [total, setTotal] = React.useState(0);
  const [partProgress, setPartProgress] = React.useState([]);
  const [error, setError] = React.useState(null);
  const [result, setResult] = React.useState(null);
  const [job, setJob] = React.useState(null);
  const inputRef = React.useRef(null);
  const [drag, setDrag] = React.useState(false);
  const [manifest, setManifest] = React.useState(null);
  const pollingRef = React.useRef(false);

  React.useEffect(() => {
    let cancelled = false;
    import("@/lib/multipartBundleUpload").then((m) => {
      // 1. Manifest for the parts table.
      m.fetchMultipartManifest().then((mf) => {
        if (!cancelled) setManifest(mf);
      }).catch(() => {});
      // 2. Rejoin any already-running background job so a browser
      //    refresh mid-import does not lose progress.
      if (pollingRef.current) return;
      m.fetchActiveBundleJob().then((activeJob) => {
        if (cancelled || !activeJob) return;
        pollingRef.current = true;
        setJob(activeJob);
        setState("VERIFYING");
        setTotal(activeJob.bytes_total || 0);
        setUploaded(activeJob.bytes_total || 0);
        m.pollBundleJob({
          jobId: activeJob.job_id,
          onUpdate: (j) => setJob(j),
        }).then((final) => {
          pollingRef.current = false;
          setJob(final);
          if (final.status === "COMPLETE") {
            setResult(final.result_summary);
            setState("DONE");
          } else {
            setError(final.error_message || final.stage_detail || "job failed");
            setState("ERROR");
          }
        }).catch((err) => {
          pollingRef.current = false;
          setError(err?.message || "polling failed");
          setState("ERROR");
        });
      }).catch(() => {});
    });
    return () => { cancelled = true; };
  }, []);

  const onSelect = (list) => {
    const arr = Array.from(list || []);
    setFiles(arr);
    setResult(null);
    setError(null);
    setJob(null);
    setState("IDLE");
  };

  const onDrop = (e) => {
    e.preventDefault();
    setDrag(false);
    if (e.dataTransfer?.files?.length) onSelect(e.dataTransfer.files);
  };

  const start = async () => {
    if (files.length !== 5) return;
    setState("UPLOADING");
    setError(null);
    setResult(null);
    setJob(null);
    setUploaded(0);
    try {
      const { uploadMultipartBundle } = await import("@/lib/multipartBundleUpload");
      const res = await uploadMultipartBundle({
        files,
        onProgress: (p) => {
          if (p.total) setTotal(p.total);
          if (p.uploaded != null) setUploaded(p.uploaded);
          if (p.partProgress) setPartProgress(p.partProgress);
          if (p.phase === "assembling") setState("VERIFYING");
          else if (p.phase === "job_enqueued" || p.phase === "job_progress") {
            setState("VERIFYING");
            if (p.job) setJob(p.job);
          } else if (p.phase === "done") {
            setState("DONE");
            if (p.job) setJob(p.job);
          } else if (p.phase === "uploading") setState("UPLOADING");
        },
      });
      setResult(res);
      setState("DONE");
    } catch (e) {
      setError(e?.response?.data?.detail || e?.message || "unknown error");
      setState("ERROR");
      if (e?.job) setJob(e.job);
    }
  };

  const pct = total ? Math.min(100, (uploaded / total) * 100) : 0;
  const active = state === "UPLOADING" || state === "VERIFYING";
  const summary = result?.results || job?.result_summary?.results;
  const ref = result?.old36_reference || job?.result_summary?.old36_reference;
  const selectedCount = files.length;

  // Server-side job stage: convert to a human label + a percentage
  // that reflects reassembly (bytes) then per-session progress.
  const stageKey = job?.current_stage || null;
  const sessionsTotal = job?.sessions_total || 11;
  const sessionsProcessed = job?.sessions_processed || 0;
  const jobPct = (() => {
    if (!job) return 0;
    if (job.status === "COMPLETE") return 100;
    if (stageKey === "IMPORTING" && sessionsTotal > 0) {
      return 50 + (sessionsProcessed / sessionsTotal) * 50;
    }
    if (stageKey === "VERIFYING_ZIP") return 45;
    if (stageKey === "VERIFYING_SHA256") return 40;
    if (stageKey === "REASSEMBLING") return 20;
    if (stageKey === "PREPARING") return 5;
    if (stageKey === "CLEANUP") return 98;
    return 0;
  })();

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-semibold tracking-wide">{t("bundle.multipart.title")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-xs text-muted-foreground">{t("bundle.multipart.subtitle")}</p>
        <p className="text-[11px] text-muted-foreground italic">{t("bundle.baseline_note")}</p>

        {manifest && (
          <div className="text-[11px] font-mono text-muted-foreground">
            <div>SHA256: {manifest.bundle_sha256}</div>
            <div>{t("bundle.multipart.expected_total")}: {fmtBytes(manifest.bundle_total_size)}</div>
          </div>
        )}

        <div
          data-testid="bundle-multipart-dropzone"
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={onDrop}
          onClick={() => inputRef.current?.click()}
          className={cn(
            "rounded-xl border border-dashed bg-card p-6 sm:p-8 transition-colors cursor-pointer flex flex-col items-center justify-center text-center",
            drag
              ? "border-[hsl(var(--focus))] bg-[hsl(var(--focus)/0.06)]"
              : "border-border hover:border-[hsl(var(--focus))]",
          )}
        >
          <Package className="h-8 w-8 text-muted-foreground" />
          <div className="mt-3 text-sm font-medium">{t("bundle.multipart.drop_hint")}</div>
          <div className="mt-1 text-xs text-muted-foreground">{t("bundle.multipart.drop_note")}</div>
          <input
            ref={inputRef}
            data-testid="bundle-multipart-file-input"
            type="file"
            multiple
            className="hidden"
            onChange={(e) => onSelect(e.target.files)}
          />
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 text-xs">
          <MetricTile
            label={t("bundle.multipart.required")}
            value={manifest?.parts?.length ?? 5}
            testid="bundle-multipart-required"
          />
          <MetricTile
            label={t("bundle.multipart.selected")}
            value={`${selectedCount} / 5`}
            testid="bundle-multipart-selected"
          />
        </div>

        {manifest && (
          <div className="overflow-x-auto rounded-lg border">
            <table className="min-w-full text-[11px]" data-testid="bundle-multipart-parts">
              <thead className="bg-muted/40 text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">#</th>
                  <th className="px-3 py-2 text-left font-medium">{t("bundle.multipart.part_name")}</th>
                  <th className="px-3 py-2 text-right font-medium">{t("bundle.multipart.expected_size")}</th>
                  <th className="px-3 py-2 text-left font-medium">{t("bundle.multipart.status")}</th>
                </tr>
              </thead>
              <tbody>
                {manifest.parts.map((p, i) => {
                  const sel = files.find((f) => f.name === p.name);
                  const pp = partProgress[i];
                  return (
                    <tr key={p.name} className="border-t">
                      <td className="px-3 py-2 font-mono">{i}</td>
                      <td className="px-3 py-2 font-mono">{p.name}</td>
                      <td className="px-3 py-2 text-right font-mono">{fmtBytes(p.size)}</td>
                      <td className="px-3 py-2">
                        {!sel ? (
                          <span className="text-muted-foreground">{t("bundle.multipart.awaiting")}</span>
                        ) : sel.size !== p.size ? (
                          <span className="text-[hsl(var(--verdict-fail))]">{t("bundle.multipart.wrong_size")}</span>
                        ) : pp?.uploadedBytes >= p.size ? (
                          <span className="text-[hsl(var(--verdict-pass))]">{t("bundle.multipart.uploaded")}</span>
                        ) : pp ? (
                          <span className="font-mono">{fmtBytes(pp.uploadedBytes)} / {fmtBytes(p.size)}</span>
                        ) : (
                          <span className="text-foreground">{t("bundle.multipart.ready")}</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <div>
          <Button
            data-testid="bundle-multipart-start-button"
            onClick={start}
            disabled={selectedCount !== 5 || active}
          >
            {active && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
            {t("bundle.multipart.start")}
          </Button>
        </div>

        {state !== "IDLE" && (
          <div className="space-y-2">
            <div className="flex items-center justify-between text-[10px] text-muted-foreground font-mono">
              <span
                data-testid="bundle-multipart-status-label"
                className="uppercase tracking-wider text-foreground"
              >
                {t(`bundle.state.${state}`)}
              </span>
              <span>
                {fmtBytes(uploaded)} / {fmtBytes(total)} · {fmtNumber(pct, { maximumFractionDigits: 1 })}%
              </span>
            </div>
            <Progress value={pct} />
          </div>
        )}

        {job && (
          <div className="space-y-2 rounded-lg border bg-muted/20 p-3">
            <div className="flex items-center justify-between text-[10px] font-mono uppercase tracking-wider">
              <span
                data-testid="bundle-multipart-job-stage"
                className="text-foreground"
              >
                {t(`bundle.job.stage.${job.current_stage}`) || job.current_stage}
              </span>
              <span
                data-testid="bundle-multipart-job-status"
                className="text-muted-foreground"
              >
                {t(`bundle.job.status.${job.status}`) || job.status}
              </span>
            </div>
            {job.stage_detail && (
              <div
                data-testid="bundle-multipart-job-detail"
                className="text-[11px] text-muted-foreground font-mono break-all"
              >
                {job.stage_detail}
              </div>
            )}
            <Progress value={jobPct} />
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[10px] font-mono text-muted-foreground">
              <div data-testid="bundle-multipart-job-id">
                job: {job.job_id?.slice(0, 12)}
              </div>
              <div data-testid="bundle-multipart-job-sessions">
                {t("bundle.job.sessions_progress")}: {job.sessions_processed} / {job.sessions_total || 11}
              </div>
              <div data-testid="bundle-multipart-job-passed">
                {t("bundle.job.passed")}: {job.sessions_passed}
              </div>
              <div data-testid="bundle-multipart-job-failed">
                {t("bundle.job.failed")}: {job.sessions_failed}
              </div>
            </div>
            {job.status === "RECOVERABLE" && (
              <Button
                data-testid="bundle-multipart-resume-button"
                onClick={async () => {
                  try {
                    const m = await import("@/lib/multipartBundleUpload");
                    const resumed = await m.resumeBundleJob(job.job_id);
                    setJob(resumed);
                    setState("VERIFYING");
                    setError(null);
                    m.pollBundleJob({
                      jobId: job.job_id,
                      onUpdate: (j) => setJob(j),
                    }).then((final) => {
                      setJob(final);
                      if (final.status === "COMPLETE") {
                        setResult(final.result_summary);
                        setState("DONE");
                      } else {
                        setError(final.error_message || final.stage_detail || "job failed");
                        setState("ERROR");
                      }
                    });
                  } catch (e) {
                    setError(e?.response?.data?.detail || e?.message || "resume failed");
                    setState("ERROR");
                  }
                }}
              >
                {t("bundle.job.resume") || "Riprendi"}
              </Button>
            )}
          </div>
        )}

        {error && (
          <div
            data-testid="bundle-multipart-error"
            className="rounded-md border border-[hsl(var(--verdict-fail)/0.4)] bg-[hsl(var(--verdict-fail)/0.06)] px-3 py-2 text-xs text-[hsl(var(--verdict-fail))]"
          >
            {t("bundle.state.ERROR")}: {error}
          </div>
        )}

        {summary && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
              <MetricTile label={t("bundle.sessions_found")} value={`${summary.total} / 11`} testid="bundle-mp-metric-found" />
              <MetricTile label={t("bundle.summary_ok")} value={summary.ok} testid="bundle-mp-metric-ok" />
              <MetricTile label={t("bundle.summary_failed")} value={summary.failed} testid="bundle-mp-metric-failed" />
              {ref && (
                <MetricTile
                  label={t("bundle.old36_available")}
                  value={`${ref.present_sessions} / ${ref.expected_sessions}`}
                  testid="bundle-mp-metric-old36"
                />
              )}
            </div>

            <div className="overflow-x-auto rounded-lg border">
              <table className="min-w-full text-xs">
                <thead className="bg-muted/40 text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">{t("bundle.table.session")}</th>
                    <th className="px-3 py-2 text-left font-medium">{t("bundle.table.verdict")}</th>
                    <th className="px-3 py-2 text-left font-medium">{t("bundle.table.duplicate")}</th>
                    <th className="px-3 py-2 text-right font-medium">{t("bundle.table.hours")}</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.sessions.map((s) => (
                    <tr key={s.session_id} className="border-t">
                      <td className="px-3 py-2 font-mono text-[11px]">{s.session_id}</td>
                      <td className="px-3 py-2">
                        {s.ok && s.result ? (
                          <VerdictBadge verdict={s.result.verdict} size="sm" />
                        ) : (
                          <span className="text-[hsl(var(--verdict-fail))] font-mono">{s.error || "\u2014"}</span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                        {s.result?.duplicate_status ? <DuplicateStateText state={s.result.duplicate_status} /> : "\u2014"}
                      </td>
                      <td className="px-3 py-2 text-right font-mono">
                        {s.result?.validated_hours != null ? fmtNumber(s.result.validated_hours, { maximumFractionDigits: 4 }) : "\u2014"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}


