import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { CheckpointRing } from "@/components/CheckpointRing";

const KEYS = ["OLD36", "NEW12", "TOTAL48", "NEW36", "TOTAL72"];

export default function CheckpointsPage() {
  const [data, setData] = useState(null);
  useEffect(() => {
    api.get("/checkpoints").then((r) => setData(r.data));
  }, []);

  const cp = data?.checkpoints || {};
  const ready = data?.data_qa_ready;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight">Checkpoints</h1>
        <p className="text-sm text-muted-foreground">
          Validated hours accounting. Duplicates never add hours twice; only PASS /
          PASS_WITH_WARNING contribute.
        </p>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-sm font-semibold tracking-wide">Progress</CardTitle>
          <span
            data-testid="checkpoints-ready-flag"
            className={`inline-flex items-center rounded-md border px-2 py-1 text-xs font-semibold ${
              ready
                ? "border-[hsl(var(--verdict-pass)/0.35)] bg-[hsl(var(--verdict-pass-bg))] text-[hsl(var(--verdict-pass))]"
                : "border-border bg-muted text-muted-foreground"
            }`}
          >
            {ready ? "72H_DATA_QA_READY: TRUE" : "72H_DATA_QA_READY: FALSE"}
          </span>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-4 place-items-center mb-6">
            {KEYS.map((k) => (
              <CheckpointRing
                key={k}
                label={k}
                hours={cp[k]?.hours || 0}
                target={cp[k]?.target || 0}
              />
            ))}
          </div>
          <div className="space-y-3">
            {KEYS.map((k) => (
              <div key={k} className="space-y-1" data-testid={`checkpoint-bar-${k.toLowerCase()}`}>
                <div className="flex items-baseline justify-between text-xs">
                  <span className="font-semibold tracking-wide">{k}</span>
                  <span className="font-mono tabular-nums">
                    {(cp[k]?.hours || 0).toFixed(2)} / {cp[k]?.target?.toFixed?.(0)} h
                  </span>
                </div>
                <Progress
                  value={
                    cp[k]?.target
                      ? Math.min(100, ((cp[k]?.hours || 0) / cp[k].target) * 100)
                      : 0
                  }
                />
              </div>
            ))}
          </div>
          <p className="mt-6 text-xs text-muted-foreground border-t pt-3">
            {data?.note}
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
