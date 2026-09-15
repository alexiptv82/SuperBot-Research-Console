import React, { useCallback, useMemo, useRef, useState } from "react";
import { useT, useLocale } from "@/lib/locale";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { VerdictBadge, DuplicateStateText } from "@/components/VerdictBadge";
import { Upload as UploadIcon, X, FileArchive, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Link } from "react-router-dom";
import { uploadChunked } from "@/lib/chunkedUpload";

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
