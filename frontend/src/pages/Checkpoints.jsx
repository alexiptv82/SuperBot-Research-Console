import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useT, useLocale } from "@/lib/locale";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { CheckpointRing } from "@/components/CheckpointRing";

const KEYS = ["OLD36", "NEW12", "TOTAL48", "NEW36", "TOTAL72"];

export default function CheckpointsPage() {
  const t = useT();
  const { fmtNumber } = useLocale();
  const [data, setData] = useState(null);
  useEffect(() => { api.get("/checkpoints").then((r) => setData(r.data)); }, []);

  const cp = data?.checkpoints || {};
  const ready = data?.data_qa_ready;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight">{t("checkpoints.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("checkpoints.subtitle")}</p>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-sm font-semibold tracking-wide">{t("checkpoints.progress")}</CardTitle>
          <span data-testid="checkpoints-ready-flag" data-ready={ready ? "true" : "false"}
            className={`inline-flex items-center rounded-md border px-2 py-1 text-xs font-semibold ${
              ready ? "border-[hsl(var(--verdict-pass)/0.35)] bg-[hsl(var(--verdict-pass-bg))] text-[hsl(var(--verdict-pass))]" : "border-border bg-muted text-muted-foreground"
            }`}>
            {ready ? t("overview.ready_true") : t("overview.ready_false")}
          </span>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-4 place-items-center mb-6">
            {KEYS.map((k) => (<CheckpointRing key={k} label={k} hours={cp[k]?.hours || 0} target={cp[k]?.target || 0} />))}
          </div>
          <div className="space-y-3">
            {KEYS.map((k) => (
              <div key={k} className="space-y-1" data-testid={`checkpoint-bar-${k.toLowerCase()}`}>
                <div className="flex items-baseline justify-between text-xs">
                  <span className="font-semibold tracking-wide">{k}</span>
                  <span className="font-mono tabular-nums">
                    {fmtNumber(cp[k]?.hours || 0, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} / {fmtNumber(cp[k]?.target || 0)} h
                  </span>
                </div>
                <Progress value={cp[k]?.target ? Math.min(100, ((cp[k]?.hours || 0) / cp[k].target) * 100) : 0} />
              </div>
            ))}
          </div>
          <p className="mt-6 text-xs text-muted-foreground border-t pt-3">{data?.note}</p>
        </CardContent>
      </Card>
    </div>
  );
}
