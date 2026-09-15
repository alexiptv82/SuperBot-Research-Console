import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import { DEFAULT_LOCALE, LOCALES, formatDate, formatNumber, getMessage } from "@/lib/i18n";

const LOCALE_STORAGE_KEY = "superbot.locale";

const LocaleCtx = createContext({
  locale: DEFAULT_LOCALE,
  setLocale: () => {},
  t: (k) => k,
  fmtDate: (iso) => iso,
  fmtNumber: (n) => String(n),
});

export function LocaleProvider({ children }) {
  const [locale, setLocaleState] = useState(() => {
    if (typeof localStorage === "undefined") return DEFAULT_LOCALE;
    const saved = localStorage.getItem(LOCALE_STORAGE_KEY);
    if (saved && LOCALES[saved]) return saved;
    // Italian is the DEFAULT. We do NOT sniff the browser locale on
    // first visit \u2014 per project spec, Italiano is the default UI.
    return DEFAULT_LOCALE;
  });

  useEffect(() => {
    if (typeof document !== "undefined") {
      document.documentElement.lang = locale;
    }
    if (typeof localStorage !== "undefined") {
      localStorage.setItem(LOCALE_STORAGE_KEY, locale);
    }
  }, [locale]);

  const setLocale = useCallback((next) => {
    if (LOCALES[next]) setLocaleState(next);
  }, []);

  const t = useCallback((key, params) => getMessage(locale, key, params), [locale]);
  const fmtDate = useCallback((iso) => formatDate(locale, iso), [locale]);
  const fmtNumber = useCallback((n, opts) => formatNumber(locale, n, opts), [locale]);

  return (
    <LocaleCtx.Provider value={{ locale, setLocale, t, fmtDate, fmtNumber }}>
      {children}
    </LocaleCtx.Provider>
  );
}

export function useLocale() {
  return useContext(LocaleCtx);
}

export function useT() {
  return useContext(LocaleCtx).t;
}
