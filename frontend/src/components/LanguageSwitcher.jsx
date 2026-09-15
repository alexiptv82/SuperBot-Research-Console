import React from "react";
import { LOCALES } from "@/lib/i18n";
import { useLocale } from "@/lib/locale";
import { Languages } from "lucide-react";

export function LanguageSwitcher() {
  const { locale, setLocale, t } = useLocale();
  return (
    <label
      className="inline-flex items-center gap-1 text-xs text-muted-foreground"
      title={t("top.language")}
    >
      <Languages className="h-3.5 w-3.5" aria-hidden="true" />
      <span className="sr-only">{t("common.language_label")}</span>
      <select
        data-testid="topbar-language-switcher"
        value={locale}
        onChange={(e) => setLocale(e.target.value)}
        className="rounded-md border bg-background px-1.5 py-0.5 text-xs"
      >
        {Object.entries(LOCALES).map(([code, meta]) => (
          <option key={code} value={code}>
            {meta.label}
          </option>
        ))}
      </select>
    </label>
  );
}
