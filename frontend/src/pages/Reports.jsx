import React from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Download } from "lucide-react";

const BACKEND = process.env.REACT_APP_BACKEND_URL || "";

function ExportCard({ fmt, label, testId, description }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-semibold tracking-wide">{label}</CardTitle>
      </CardHeader>
      <CardContent className="flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">{description}</p>
        <Button asChild variant="secondary" data-testid={testId}>
          <a href={`${BACKEND}/api/reports/export?fmt=${fmt}`}>
            <Download className="h-4 w-4 mr-1" /> Download
          </a>
        </Button>
      </CardContent>
    </Card>
  );
}

export default function ReportsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight">QA Reports</h1>
        <p className="text-sm text-muted-foreground">
          Registry-wide exports of the latest QA run per session. All fields from §12.3 are
          included.
        </p>
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        <ExportCard
          fmt="json"
          label="JSON"
          description="Machine-friendly, includes nested checks and manifest."
          testId="reports-export-json-button"
        />
        <ExportCard
          fmt="csv"
          label="CSV"
          description="Flat spreadsheet-friendly export; nested fields as JSON strings."
          testId="reports-export-csv-button"
        />
        <ExportCard
          fmt="md"
          label="Markdown"
          description="Human-readable summary suitable for handoff / ChatGPT audit."
          testId="reports-export-md-button"
        />
      </div>
    </div>
  );
}
