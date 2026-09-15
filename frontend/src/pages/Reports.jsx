import React from "react";
import { useT } from "@/lib/locale";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Download } from "lucide-react";

const BACKEND = process.env.REACT_APP_BACKEND_URL || "";

function ExportCard({ fmt, label, testId, description, downloadLabel }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-semibold tracking-wide">{label}</CardTitle>
      </CardHeader>
      <CardContent className="flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">{description}</p>
        <Button asChild variant="secondary" data-testid={testId}>
          <a href={`${BACKEND}/api/reports/export?fmt=${fmt}`}>
            <Download className="h-4 w-4 mr-1" /> {downloadLabel}
          </a>
        </Button>
      </CardContent>
    </Card>
  );
}

export default function ReportsPage() {
  const t = useT();
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight">{t("reports.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("reports.subtitle")}</p>
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        <ExportCard fmt="json" label="JSON" description={t("reports.json_desc")} downloadLabel={t("reports.download")} testId="reports-export-json-button" />
        <ExportCard fmt="csv" label="CSV" description={t("reports.csv_desc")} downloadLabel={t("reports.download")} testId="reports-export-csv-button" />
        <ExportCard fmt="md" label="Markdown" description={t("reports.md_desc")} downloadLabel={t("reports.download")} testId="reports-export-md-button" />
      </div>
    </div>
  );
}
