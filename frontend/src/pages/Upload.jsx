import React, { useCallback, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useT } from "@/lib/locale";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { VerdictBadge, DuplicateStateText } from "@/components/VerdictBadge";
import { Upload as UploadIcon, X, FileArchive, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Link } from "react-router-dom";

const CHECKPOINTS = ["", "OLD36", "NEW12", "NEW36"];

export default function UploadPage() {
  const t = useT();
  const [files, setFiles] = useState([]);
  const [drag, setDrag] = useState(false);
  const [retainRaw, setRetainRaw] = useState(false);
  const [checkpoint, setCheckpoint] = useState("");
  const [busy, setBusy] = useState(false);
  const inputRef = useRef(null);

  const addFiles = useCallback((selected) => {
    const arr = Array.from(selected || []);
    setFiles((prev) => [
      ...prev,
      ...arr.map((f) => ({
        file: f,
        id: `${f.name}:${f.size}:${Math.random().toString(36).slice(2, 8)}`,
        state: "queued",
        result: null,
        error: null,
      })),
    ]);
  }, []);

  const onDrop = (e) => {
    e.preventDefault();
    setDrag(false);
    if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files);
  };

  const remove = (id) => setFiles((prev) => prev.filter((f) => f.id !== id));

  const upload = async () => {
    if (!files.length) return;
    setBusy(true);
    for (const item of files) {
      if (item.state === "done") continue;
      setFiles((prev) => prev.map((f) => (f.id === item.id ? { ...f, state: "uploading" } : f)));
      const fd = new FormData();
      fd.append("files", item.file, item.file.name);
      fd.append("retain_raw", retainRaw ? "true" : "false");
      if (checkpoint) fd.append("checkpoint_hint", checkpoint);
      try {
        const r = await api.post("/sessions/upload", fd, {
          headers: { "Content-Type": "multipart/form-data" },
        });
        const res = r.data?.results?.[0];
        setFiles((prev) =>
          prev.map((f) => (f.id === item.id ? { ...f, state: "done", result: res } : f)),
        );
      } catch (e) {
        setFiles((prev) =>
          prev.map((f) =>
            f.id === item.id
              ? { ...f, state: "error", error: e?.response?.data?.detail || "Upload failed" }
              : f,
          ),
        );
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
                  <option key={c} value={c}>
                    {c || t("upload.checkpoint_auto")}
                  </option>
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
          <CardContent className="space-y-2">
            {files.map((f) => (
              <div key={f.id} data-testid="upload-file-row" className="flex items-center gap-3 rounded-lg border bg-background/40 px-3 py-2">
                <FileArchive className="h-4 w-4 text-muted-foreground" />
                <div className="flex-1 min-w-0">
                  <div className="text-sm truncate">{f.file.name}</div>
                  <div className="text-[10px] text-muted-foreground font-mono">
                    {(f.file.size / (1024 * 1024)).toFixed(2)} MB
                    {f.result?.file_sha256 && (
                      <span className="ml-2">sha256={f.result.file_sha256.slice(0, 12)}…</span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  {f.state === "queued" && <span className="text-xs text-muted-foreground">{t("upload.state_queued")}</span>}
                  {f.state === "uploading" && (
                    <span className="text-xs text-muted-foreground flex items-center gap-1">
                      <Loader2 className="h-3 w-3 animate-spin" /> {t("upload.state_validating")}
                    </span>
                  )}
                  {f.state === "done" && f.result && (
                    <>
                      <span className="text-[10px] text-muted-foreground uppercase">
                        <DuplicateStateText state={f.result.duplicate_status} />
                      </span>
                      <VerdictBadge verdict={f.result.verdict} size="sm" />
                      {f.result.session_id && (
                        <Link to={`/session/${encodeURIComponent(f.result.session_id)}`} className="text-xs text-[hsl(var(--focus))] hover:underline" data-testid="upload-file-view">
                          {t("upload.file_view")}
                        </Link>
                      )}
                    </>
                  )}
                  {f.state === "error" && (
                    <span className="text-xs text-[hsl(var(--verdict-fail))]">{f.error}</span>
                  )}
                  <Button variant="ghost" size="icon" onClick={() => remove(f.id)} aria-label={t("upload.remove")} data-testid="upload-file-remove-button">
                    <X className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
