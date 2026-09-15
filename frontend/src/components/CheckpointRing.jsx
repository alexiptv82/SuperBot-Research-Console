import React from "react";

export function CheckpointRing({ label, hours, target, size = 120, testId }) {
  const pct = target > 0 ? Math.min(1, hours / target) : 0;
  const r = size / 2 - 10;
  const c = 2 * Math.PI * r;
  const dash = c * pct;
  return (
    <div
      className="flex flex-col items-center gap-2"
      data-testid={testId || `checkpoint-ring-${(label || "").toLowerCase()}`}
    >
      <svg width={size} height={size} className="checkpoint-ring -rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="hsl(var(--border))"
          strokeWidth={8}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="hsl(var(--focus))"
          strokeWidth={8}
          strokeLinecap="round"
          strokeDasharray={`${dash} ${c - dash}`}
        />
      </svg>
      <div className="-mt-[76%] pointer-events-none flex flex-col items-center" style={{ marginTop: `-${size * 0.62}px` }}>
        <div className="font-mono text-base tabular-nums font-semibold">
          {hours.toFixed(1)}
        </div>
        <div className="text-[10px] text-muted-foreground">/ {target.toFixed(0)}h</div>
      </div>
      <div style={{ marginTop: `${size * 0.15}px` }} className="text-xs font-semibold tracking-wide">
        {label}
      </div>
    </div>
  );
}
