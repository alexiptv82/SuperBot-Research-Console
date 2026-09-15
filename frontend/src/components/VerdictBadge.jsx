import React from "react";
import { cn } from "@/lib/utils";

const STYLE = {
  PASS: {
    badge: "border-[hsl(var(--verdict-pass)/0.35)] bg-[hsl(var(--verdict-pass-bg))] text-[hsl(var(--verdict-pass))]",
    dot: "bg-[hsl(var(--verdict-pass))]",
    label: "PASS",
  },
  PASS_WITH_WARNING: {
    badge: "border-[hsl(var(--verdict-warn)/0.35)] bg-[hsl(var(--verdict-warn-bg))] text-[hsl(var(--verdict-warn))]",
    dot: "bg-[hsl(var(--verdict-warn))]",
    label: "PASS WITH WARNING",
  },
  FAIL: {
    badge: "border-[hsl(var(--verdict-fail)/0.35)] bg-[hsl(var(--verdict-fail-bg))] text-[hsl(var(--verdict-fail))]",
    dot: "bg-[hsl(var(--verdict-fail))]",
    label: "FAIL",
  },
  UNRESOLVED: {
    badge: "border-[hsl(var(--verdict-unresolved)/0.35)] bg-[hsl(var(--verdict-unresolved-bg))] text-[hsl(var(--verdict-unresolved))]",
    dot: "bg-[hsl(var(--verdict-unresolved))]",
    label: "UNRESOLVED",
  },
};

export function VerdictBadge({ verdict, size = "md", className }) {
  const v = STYLE[verdict] || STYLE.UNRESOLVED;
  const testid = `verdict-badge-${(verdict || "unresolved").toLowerCase().replaceAll("_", "-")}`;
  const sz = size === "sm" ? "px-2 py-0.5 text-[10px]" : "px-2.5 py-1 text-xs";
  return (
    <span
      data-testid={testid}
      className={cn(
        "inline-flex items-center gap-2 rounded-md border font-semibold tracking-wide whitespace-nowrap",
        v.badge,
        sz,
        className,
      )}
    >
      <span className={cn("h-2 w-2 rounded-full", v.dot)} />
      {v.label}
    </span>
  );
}
