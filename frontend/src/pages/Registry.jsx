import React, { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { VerdictBadge } from "@/components/VerdictBadge";
import { Button } from "@/components/ui/button";
import { RefreshCcw } from "lucide-react";

const STATUSES = ["ALL", "PASS", "PASS_WITH_WARNING", "FAIL", "UNRESOLVED"];

export default function RegistryPage() {
  const [rows, setRows] = useState([]);
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState("ALL");
  const [loading, setLoading] = useState(false);

  const load = () => {
    setLoading(true);
    api
      .get("/sessions")
      .then((r) => setRows(r.data?.sessions || []))
      .finally(() => setLoading(false));
  };
  useEffect(() => {
    load();
  }, []);

  const filtered = useMemo(() => {
    return rows.filter((r) => {
      if (statusFilter !== "ALL" && r.operational_status !== statusFilter) return false;
      if (q && !`${r.session_id} ${r.original_filename}`.toLowerCase().includes(q.toLowerCase())) return false;
      return true;
    });
  }, [rows, q, statusFilter]);

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight">Session Registry</h1>
          <p className="text-sm text-muted-foreground">
            One row per session; the latest QA run is shown. Reprocessing creates a new run
            without overwriting history.
          </p>
        </div>
        <Button variant="secondary" onClick={load} data-testid="registry-refresh">
          <RefreshCcw className="h-4 w-4 mr-2" /> Refresh
        </Button>
      </div>

      <Card>
        <CardHeader className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <CardTitle className="text-sm font-semibold tracking-wide">
            {filtered.length} of {rows.length} sessions
          </CardTitle>
          <div className="flex flex-col sm:flex-row gap-2">
            <Input
              placeholder="Search session_id or filename…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              className="sm:w-64"
              data-testid="registry-search-input"
            />
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="rounded-md border bg-background px-2 py-1 text-sm"
              data-testid="registry-status-filter"
            >
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
        </CardHeader>
        <CardContent className="p-0 overflow-x-auto">
          <Table data-testid="registry-table">
            <TableHeader>
              <TableRow>
                <TableHead>Verdict</TableHead>
                <TableHead>Session ID</TableHead>
                <TableHead>Filename</TableHead>
                <TableHead>Duplicate</TableHead>
                <TableHead>Checkpoint</TableHead>
                <TableHead>Hours</TableHead>
                <TableHead>Uploaded</TableHead>
                <TableHead>Retained</TableHead>
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading && (
                <TableRow>
                  <TableCell colSpan={9} className="py-8 text-center text-sm text-muted-foreground">
                    Loading…
                  </TableCell>
                </TableRow>
              )}
              {!loading && filtered.length === 0 && (
                <TableRow>
                  <TableCell colSpan={9} className="py-8 text-center text-sm text-muted-foreground">
                    No sessions match. Upload a ZIP to begin.
                  </TableCell>
                </TableRow>
              )}
              {filtered.map((r) => (
                <TableRow key={r.id} data-testid="registry-row">
                  <TableCell>
                    <VerdictBadge verdict={r.operational_status} size="sm" />
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    <Link
                      to={`/session/${encodeURIComponent(r.session_id)}`}
                      className="hover:underline"
                    >
                      {r.session_id}
                    </Link>
                  </TableCell>
                  <TableCell className="text-xs truncate max-w-[220px]">{r.original_filename}</TableCell>
                  <TableCell className="text-[10px] text-muted-foreground">{r.duplicate_status}</TableCell>
                  <TableCell className="text-xs">{r.checkpoint_hint || "—"}</TableCell>
                  <TableCell className="font-mono tabular-nums text-xs">{(r.validated_hours || 0).toFixed(2)}</TableCell>
                  <TableCell className="font-mono text-[10px] text-muted-foreground">
                    {r.uploaded_at?.replace("T", " ").slice(0, 19)}
                  </TableCell>
                  <TableCell className="text-xs">
                    {r.retained ? (
                      <span className="text-[hsl(var(--verdict-warn))]">retained</span>
                    ) : (
                      <span className="text-muted-foreground">deleted</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <Button asChild variant="ghost" size="sm" data-testid="registry-row-view-button">
                      <Link to={`/session/${encodeURIComponent(r.session_id)}`}>Open</Link>
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
