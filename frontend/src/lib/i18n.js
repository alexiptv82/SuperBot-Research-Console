// Lightweight i18n for SuperBot Research Console V1.
// Two locales: it (Italian, default) and en (English).
// No external framework. Internal machine values (PASS / FAIL /
// UNRESOLVED / OLD36 / TOTAL72 / SHA256 / FrozenAnalysisEngine ...) are
// NEVER translated — they remain in DB, API payloads, enums, filenames
// and constants. This module only maps user-visible strings.

export const LOCALES = {
  it: { label: "Italiano", short: "IT" },
  en: { label: "English", short: "EN" },
};

export const DEFAULT_LOCALE = "it";

const MESSAGES = {
  it: {
    // Verdicts (UI display only — internal machine values PASS /
    // PASS_WITH_WARNING / FAIL / UNRESOLVED remain identical everywhere).
    "verdict.PASS": "SUPERATO",
    "verdict.PASS_WITH_WARNING": "SUPERATO CON AVVISO",
    "verdict.FAIL": "ERRORE",
    "verdict.UNRESOLVED": "DA RISOLVERE",

    // Duplicate states (UI display only).
    "dup.NEW": "NUOVO",
    "dup.EXACT_DUPLICATE": "DUPLICATO ESATTO",
    "dup.SAME_SESSION_DIFFERENT_FILE": "STESSA SESSIONE, FILE DIVERSO",
    "dup.CONFLICT": "CONFLITTO",

    // Navigation
    "nav.overview": "Panoramica",
    "nav.upload": "Carica sessioni",
    "nav.registry": "Registro sessioni",
    "nav.reports": "Report QA",
    "nav.checkpoints": "Milestone",
    "nav.policy": "Regole di progetto",
    "nav.audit": "Log di sistema",

    // Top bar
    "top.subtitle": "Console QA deterministica — nessun trading, nessuna credenziale di exchange",
    "top.sessions_count": "{n} sessioni",
    "top.engine_status": "Motore: {status}",
    "top.logout": "Esci",
    "top.theme": "Cambia tema",
    "top.language": "Lingua",

    // Login
    "login.title": "Accesso proprietario",
    "login.subtitle":
      "Console QA deterministica a utente singolo. Caricamenti e risultati sono registrati in un audit trail immutabile.",
    "login.password": "Password",
    "login.submit": "Accedi",
    "login.submitting": "Accesso in corso…",
    "login.error": "Password errata. Accesso negato.",
    "login.footer":
      "L’accesso è protetto da un cookie di sessione firmato. La password è fornita tramite la variabile d’ambiente SUPERBOT_PASSWORD. Modificala prima di qualsiasi deploy in produzione.",
    "login.show_password": "Mostra password",
    "login.hide_password": "Nascondi password",

    // Overview
    "overview.title": "Panoramica",
    "overview.subtitle": "Ore validate verso la milestone operativa/dati 72H.",
    "overview.upload_cta": "Carica nuove sessioni",
    "overview.registry_cta": "Registro",
    "overview.checkpoint_progress": "Avanzamento milestone",
    "overview.ready_true": "72H_DATA_QA_READY: VERO",
    "overview.ready_false": "72H_DATA_QA_READY: FALSO",
    "overview.verdict_distribution": "Distribuzione verdetti",
    "overview.recent_sessions": "Sessioni recenti",
    "overview.recent_empty": "Nessuna sessione ancora. Carica uno ZIP 3H per iniziare.",
    "overview.engine_title": "FrozenAnalysisEngine",
    "overview.engine_note":
      "V1 dichiara 72H_DATA_QA_READY solamente; mai CONFIRMED_EXECUTION_STRUCTURE.",
    "overview.engine_cta": "Esegui analisi 72H (disabilitato)",
    "common.view": "Apri",
    "common.view_arrow": "Apri →",

    // Upload
    "upload.title": "Carica sessioni",
    "upload.subtitle":
      "Trascina uno o più ZIP di sessione MultiVenue 3H. Ogni file viene validato deterministicamente contro l’SHA256 del collector congelato e le regole di sessione.",
    "upload.ingest": "Acquisizione",
    "upload.drop_hint": "Trascina qui i file ZIP o clicca per selezionarli",
    "upload.drop_note":
      "I file restano in memoria durante la QA; la ritenzione è controllata qui sotto.",
    "upload.retain": "Conserva lo ZIP originale dopo la validazione",
    "upload.checkpoint_hint": "Milestone suggerita:",
    "upload.checkpoint_auto": "(auto dal nome file)",
    "upload.start": "Valida",
    "upload.queue": "Coda",
    "upload.state_queued": "in coda",
    "upload.state_validating": "validazione…",
    "upload.remove": "Rimuovi",
    "upload.file_view": "Apri",
    "upload.retry": "tentativo #{n}",
    "upload.err.unknown": "Errore di caricamento sconosciuto",
    "upload.state.QUEUED": "IN CODA",
    "upload.state.PREPARING": "PREPARAZIONE",
    "upload.state.UPLOADING": "CARICAMENTO",
    "upload.state.ASSEMBLING": "ASSEMBLAGGIO",
    "upload.state.UPLOADED": "CARICATO",
    "upload.state.QA": "VALIDAZIONE QA",
    "upload.state.DONE": "COMPLETATO",
    "upload.state.ERROR": "ERRORE",
    "upload.mode.single": "Sessione singola",
    "upload.mode.bundle": "Importa bundle OLD36",
    "bundle.title": "Importa bundle OLD36",
    "bundle.subtitle":
      "Un unico ZIP che contiene le 11 sessioni storiche OLD36_REFERENCE. Solo per il recupero del validatore \u2014 non modifica TOTAL72, non tocca NEW36.",
    "bundle.drop_hint": "Trascina qui il bundle OLD36 o clicca per selezionarlo",
    "bundle.drop_note":
      "File singolo .zip fino a 3 GiB. Le sessioni interne devono coincidere esattamente con la lista OLD36_REFERENCE.",
    "bundle.start": "Importa bundle",
    "bundle.selected": "Bundle selezionato",
    "bundle.size": "Dimensione totale",
    "bundle.state.PREPARING": "PREPARAZIONE",
    "bundle.state.UPLOADING": "CARICAMENTO",
    "bundle.state.VERIFYING": "VERIFICA ARCHIVIO",
    "bundle.state.PROCESSING": "PROCESSAMENTO SESSIONI",
    "bundle.state.DONE": "COMPLETATO",
    "bundle.state.ERROR": "ERRORE",
    "bundle.sessions_found": "Sessioni trovate",
    "bundle.summary_ok": "Sessioni importate",
    "bundle.summary_failed": "Sessioni non riuscite",
    "bundle.old36_available": "OLD36 raw disponibili",
    "bundle.old36_hours": "Ore nominali disponibili",
    "bundle.baseline_note":
      "OLD36 baseline resta fisso a 36.0h. Milestone e TOTAL72 non cambiano.",
    "bundle.table.session": "Sessione",
    "bundle.table.verdict": "Verdetto",
    "bundle.table.duplicate": "Duplicato",
    "bundle.table.hours": "Ore convalidate",
    "bundle.table.detail": "Dettaglio",
    "bundle.submode.single": "Bundle singolo",
    "bundle.submode.multipart": "Bundle multipart",
    "bundle.multipart.title": "Importa bundle multipart (5 parti)",
    "bundle.multipart.subtitle":
      "Carica le 5 parti binarie di OLD36_REFERENCE_BUNDLE.zip. Il server verifica ordine, dimensioni e SHA256 prima di ricostruire il bundle originale.",
    "bundle.multipart.drop_hint": "Trascina le 5 parti qui o clicca per selezionarle",
    "bundle.multipart.drop_note":
      "Selezione multipla richiesta: part-00, part-01, part-02, part-03, part-04. L\u2019ordine \u00e8 imposto dal server.",
    "bundle.multipart.required": "Parti richieste",
    "bundle.multipart.selected": "Parti selezionate",
    "bundle.multipart.expected_total": "Dimensione totale attesa",
    "bundle.multipart.part_name": "Nome parte",
    "bundle.multipart.expected_size": "Dimensione attesa",
    "bundle.multipart.status": "Stato",
    "bundle.multipart.awaiting": "in attesa",
    "bundle.multipart.wrong_size": "dimensione errata",
    "bundle.multipart.ready": "pronta",
    "bundle.multipart.uploaded": "caricata",
    "bundle.multipart.start": "Ricostruisci e importa",

    // Registry
    "registry.title": "Registro sessioni",
    "registry.subtitle":
      "Una riga per sessione; viene mostrata l’ultima esecuzione QA. La ri-elaborazione crea una nuova esecuzione senza sovrascrivere lo storico.",
    "registry.refresh": "Aggiorna",
    "registry.count": "{shown} di {total} sessioni",
    "registry.search": "Cerca session_id o nome file…",
    "registry.filter_all": "TUTTI",
    "registry.col_verdict": "Verdetto",
    "registry.col_session_id": "ID sessione",
    "registry.col_filename": "Nome file",
    "registry.col_duplicate": "Duplicato",
    "registry.col_checkpoint": "Milestone",
    "registry.col_hours": "Ore",
    "registry.col_uploaded": "Caricato il",
    "registry.col_retained": "Conservato",
    "registry.col_actions": "Azioni",
    "registry.retained_yes": "conservato",
    "registry.retained_no": "eliminato",
    "registry.loading": "Caricamento…",
    "registry.empty": "Nessuna sessione corrisponde. Carica uno ZIP per iniziare.",
    "registry.open": "Apri",

    // Session Detail
    "detail.back": "indietro",
    "detail.runs_count": "{n} esecuzioni QA",
    "detail.download_json": "JSON",
    "detail.download_csv": "CSV",
    "detail.download_md": "Markdown",
    "detail.reprocess": "Ri-elabora",
    "detail.reprocess_tooltip_ok": "Ri-elabora lo ZIP originale conservato",
    "detail.reprocess_tooltip_ko": "Nessuno ZIP originale conservato",
    "detail.latest": "Ultima esecuzione QA",
    "detail.section.identity": "Identità",
    "detail.section.verdict": "Verdetto e motivazioni",
    "detail.section.runtime": "Runtime",
    "detail.section.dataset": "Struttura dataset",
    "detail.section.reconnects": "Riconnessioni",
    "detail.section.retention": "Ritenzione",
    "detail.section.checks": "Dettaglio controlli",
    "detail.section.manifest": "Manifest originale",
    "detail.reprocess_history": "Storico ri-elaborazioni",
    "detail.field.failure_reasons": "Motivazioni di errore",
    "detail.field.warnings": "Avvisi",
    "detail.field.missing_fields": "Campi critici mancanti",
    "detail.field.retained": "Conservato",
    "detail.field.retention_reason": "Motivo ritenzione",
    "detail.retained_yes": "SÌ",
    "detail.retained_no": "NO",
    "detail.reprocess_failed": "Ri-elaborazione fallita",

    // Reports
    "reports.title": "Report QA",
    "reports.subtitle":
      "Export dell’intero registro (ultima esecuzione QA per sessione). Tutti i campi previsti da §12.3 sono inclusi.",
    "reports.json_desc":
      "Compatibile con macchine, include controlli annidati e manifest.",
    "reports.csv_desc":
      "Foglio elettronico piatto; i campi annidati sono serializzati come stringhe JSON.",
    "reports.md_desc":
      "Riassunto leggibile, adatto ad handoff / audit ChatGPT.",
    "reports.download": "Scarica",

    // Checkpoints
    "checkpoints.title": "Milestone",
    "checkpoints.subtitle":
      "Contabilità delle ore validate. I duplicati non aggiungono mai ore due volte; contribuiscono solo SUPERATO / SUPERATO CON AVVISO.",
    "checkpoints.progress": "Avanzamento",

    // Policy
    "policy.title": "Regole di progetto",
    "policy.subtitle":
      "Regole congelate dell’handoff SuperBot Trading Project. Le misure deterministiche successive prevalgono su questo documento — ma in V1 nulla cambia in silenzio.",
    "policy.collector_title": "SHA256 collector congelato",
    "policy.collector_note":
      "Ogni ZIP caricato viene confrontato con questo hash. Mancata corrispondenza → ERRORE.",
    "policy.copy": "Copia",
    "policy.copied": "Copiato",
    "policy.frozen_rules": "Regole congelate",
    "policy.v1_prohibitions": "Divieti V1",
    "policy.verdict_vocab": "Vocabolario verdetti",
    "policy.duplicate_states": "Stati duplicato",
    "policy.frozen_horizons": "Orizzonti congelati",
    "policy.hurdle": "Soglia economica: {n} bps",
    "policy.quantiles": "Quantili: {list}",
    "policy.engine_title": "FrozenAnalysisEngine",

    // Audit
    "audit.title": "Log di sistema / Audit",
    "audit.subtitle":
      "Ledger append-only di ogni caricamento, verdetto, decisione di ritenzione e ri-elaborazione. Le righe non vengono mai modificate o eliminate.",
    "audit.events_count": "{n} eventi",
    "audit.search": "Cerca…",
    "audit.all_types": "Tutti i tipi",
    "audit.all_outcomes": "Tutti gli esiti",
    "audit.refresh": "Aggiorna",
    "audit.empty": "Nessun evento ancora.",

    // Common
    "common.loading": "Caricamento…",
    "common.none": "—",
    "common.language_label": "Lingua",
  },

  en: {
    "verdict.PASS": "PASS",
    "verdict.PASS_WITH_WARNING": "PASS WITH WARNING",
    "verdict.FAIL": "FAIL",
    "verdict.UNRESOLVED": "UNRESOLVED",

    "dup.NEW": "NEW",
    "dup.EXACT_DUPLICATE": "EXACT DUPLICATE",
    "dup.SAME_SESSION_DIFFERENT_FILE": "SAME SESSION, DIFFERENT FILE",
    "dup.CONFLICT": "CONFLICT",

    "nav.overview": "Overview",
    "nav.upload": "Upload Sessions",
    "nav.registry": "Session Registry",
    "nav.reports": "QA Reports",
    "nav.checkpoints": "Checkpoints",
    "nav.policy": "Project Policy",
    "nav.audit": "System / Audit Log",

    "top.subtitle": "Deterministic QA console — no trading, no exchange creds",
    "top.sessions_count": "{n} sessions",
    "top.engine_status": "Engine: {status}",
    "top.logout": "Logout",
    "top.theme": "Toggle theme",
    "top.language": "Language",

    "login.title": "Owner sign-in",
    "login.subtitle":
      "This is a single-owner deterministic QA console. Uploads and results are logged to an immutable audit trail.",
    "login.password": "Password",
    "login.submit": "Sign in",
    "login.submitting": "Signing in…",
    "login.error": "Invalid password. Access denied.",
    "login.footer":
      "Access is protected by a signed session cookie. The password is provisioned via the SUPERBOT_PASSWORD environment variable. Change it before any production deployment.",
    "login.show_password": "Show password",
    "login.hide_password": "Hide password",

    "overview.title": "Overview",
    "overview.subtitle": "Validated hours toward the 72H operational/data QA milestone.",
    "overview.upload_cta": "Upload new sessions",
    "overview.registry_cta": "Registry",
    "overview.checkpoint_progress": "Checkpoint progress",
    "overview.ready_true": "72H_DATA_QA_READY: TRUE",
    "overview.ready_false": "72H_DATA_QA_READY: FALSE",
    "overview.verdict_distribution": "Verdict distribution",
    "overview.recent_sessions": "Recent sessions",
    "overview.recent_empty": "No sessions yet. Upload a 3H session ZIP to begin.",
    "overview.engine_title": "FrozenAnalysisEngine",
    "overview.engine_note":
      "V1 declares 72H_DATA_QA_READY only; never CONFIRMED_EXECUTION_STRUCTURE.",
    "overview.engine_cta": "Run 72H analysis (disabled)",
    "common.view": "View",
    "common.view_arrow": "View →",

    "upload.title": "Upload Sessions",
    "upload.subtitle":
      "Drag one or more MultiVenue 3H session ZIPs. Each file is validated deterministically against the frozen collector SHA256 and session policy.",
    "upload.ingest": "Ingest",
    "upload.drop_hint": "Drop ZIP files here or click to browse",
    "upload.drop_note":
      "Files stay in memory during QA; retention is controlled below.",
    "upload.retain": "Retain raw ZIP after validation",
    "upload.checkpoint_hint": "Checkpoint hint:",
    "upload.checkpoint_auto": "(auto from filename)",
    "upload.start": "Validate",
    "upload.queue": "Queue",
    "upload.state_queued": "queued",
    "upload.state_validating": "validating…",
    "upload.remove": "Remove",
    "upload.file_view": "View",
    "upload.retry": "attempt #{n}",
    "upload.err.unknown": "Unknown upload error",
    "upload.state.QUEUED": "QUEUED",
    "upload.state.PREPARING": "PREPARING",
    "upload.state.UPLOADING": "UPLOADING",
    "upload.state.ASSEMBLING": "ASSEMBLING",
    "upload.state.UPLOADED": "UPLOADED",
    "upload.state.QA": "QA VALIDATION",
    "upload.state.DONE": "COMPLETED",
    "upload.state.ERROR": "ERROR",
    "upload.mode.single": "Single session",
    "upload.mode.bundle": "Import OLD36 bundle",
    "bundle.title": "Import OLD36 bundle",
    "bundle.subtitle":
      "One ZIP containing the 11 historical OLD36_REFERENCE sessions. Recovery-only \u2014 does not affect TOTAL72 and does not touch NEW36.",
    "bundle.drop_hint": "Drop the OLD36 bundle here or click to browse",
    "bundle.drop_note":
      "Single .zip file up to 3 GiB. Inner sessions must match the OLD36_REFERENCE list exactly.",
    "bundle.start": "Import bundle",
    "bundle.selected": "Selected bundle",
    "bundle.size": "Total size",
    "bundle.state.PREPARING": "PREPARING",
    "bundle.state.UPLOADING": "UPLOADING",
    "bundle.state.VERIFYING": "VERIFYING ARCHIVE",
    "bundle.state.PROCESSING": "PROCESSING SESSIONS",
    "bundle.state.DONE": "COMPLETED",
    "bundle.state.ERROR": "ERROR",
    "bundle.sessions_found": "Sessions found",
    "bundle.summary_ok": "Sessions imported",
    "bundle.summary_failed": "Sessions failed",
    "bundle.old36_available": "OLD36 raw available",
    "bundle.old36_hours": "Nominal hours available",
    "bundle.baseline_note":
      "OLD36 baseline stays at 36.0h. Milestones and TOTAL72 are unchanged.",
    "bundle.table.session": "Session",
    "bundle.table.verdict": "Verdict",
    "bundle.table.duplicate": "Duplicate",
    "bundle.table.hours": "Validated hours",
    "bundle.table.detail": "Detail",
    "bundle.submode.single": "Single bundle",
    "bundle.submode.multipart": "Multipart bundle",
    "bundle.multipart.title": "Import multipart bundle (5 parts)",
    "bundle.multipart.subtitle":
      "Upload the 5 raw binary parts of OLD36_REFERENCE_BUNDLE.zip. The server enforces order, sizes and SHA256 before reassembling the original bundle.",
    "bundle.multipart.drop_hint": "Drop the 5 parts here or click to browse",
    "bundle.multipart.drop_note":
      "Multi-select required: part-00, part-01, part-02, part-03, part-04. Order is enforced server-side.",
    "bundle.multipart.required": "Parts required",
    "bundle.multipart.selected": "Parts selected",
    "bundle.multipart.expected_total": "Expected total size",
    "bundle.multipart.part_name": "Part name",
    "bundle.multipart.expected_size": "Expected size",
    "bundle.multipart.status": "Status",
    "bundle.multipart.awaiting": "awaiting",
    "bundle.multipart.wrong_size": "wrong size",
    "bundle.multipart.ready": "ready",
    "bundle.multipart.uploaded": "uploaded",
    "bundle.multipart.start": "Reassemble and import",

    "registry.title": "Session Registry",
    "registry.subtitle":
      "One row per session; the latest QA run is shown. Reprocessing creates a new run without overwriting history.",
    "registry.refresh": "Refresh",
    "registry.count": "{shown} of {total} sessions",
    "registry.search": "Search session_id or filename…",
    "registry.filter_all": "ALL",
    "registry.col_verdict": "Verdict",
    "registry.col_session_id": "Session ID",
    "registry.col_filename": "Filename",
    "registry.col_duplicate": "Duplicate",
    "registry.col_checkpoint": "Checkpoint",
    "registry.col_hours": "Hours",
    "registry.col_uploaded": "Uploaded",
    "registry.col_retained": "Retained",
    "registry.col_actions": "Actions",
    "registry.retained_yes": "retained",
    "registry.retained_no": "deleted",
    "registry.loading": "Loading…",
    "registry.empty": "No sessions match. Upload a ZIP to begin.",
    "registry.open": "Open",

    "detail.back": "back",
    "detail.runs_count": "{n} QA run(s)",
    "detail.download_json": "JSON",
    "detail.download_csv": "CSV",
    "detail.download_md": "Markdown",
    "detail.reprocess": "Reprocess",
    "detail.reprocess_tooltip_ok": "Reprocess retained raw ZIP",
    "detail.reprocess_tooltip_ko": "No retained raw ZIP",
    "detail.latest": "Latest QA run",
    "detail.section.identity": "Identity",
    "detail.section.verdict": "Verdict & reasons",
    "detail.section.runtime": "Runtime",
    "detail.section.dataset": "Dataset structure",
    "detail.section.reconnects": "Reconnects",
    "detail.section.retention": "Retention",
    "detail.section.checks": "Per-check breakdown",
    "detail.section.manifest": "Manifest raw",
    "detail.reprocess_history": "Reprocess history",
    "detail.field.failure_reasons": "Failure reasons",
    "detail.field.warnings": "Warnings",
    "detail.field.missing_fields": "Missing critical fields",
    "detail.field.retained": "Retained",
    "detail.field.retention_reason": "Retention reason",
    "detail.retained_yes": "YES",
    "detail.retained_no": "NO",
    "detail.reprocess_failed": "Reprocess failed",

    "reports.title": "QA Reports",
    "reports.subtitle":
      "Registry-wide exports of the latest QA run per session. All fields from §12.3 are included.",
    "reports.json_desc":
      "Machine-friendly, includes nested checks and manifest.",
    "reports.csv_desc":
      "Flat spreadsheet-friendly export; nested fields as JSON strings.",
    "reports.md_desc":
      "Human-readable summary suitable for handoff / ChatGPT audit.",
    "reports.download": "Download",

    "checkpoints.title": "Checkpoints",
    "checkpoints.subtitle":
      "Validated hours accounting. Duplicates never add hours twice; only PASS / PASS_WITH_WARNING contribute.",
    "checkpoints.progress": "Progress",

    "policy.title": "Project Policy",
    "policy.subtitle":
      "Frozen rules from the SuperBot Trading Project handoff. Later deterministic measurements supersede this document — but nothing here changes silently in V1.",
    "policy.collector_title": "Frozen collector SHA256",
    "policy.collector_note":
      "Every uploaded ZIP is compared against this hash. Mismatch → FAIL.",
    "policy.copy": "Copy",
    "policy.copied": "Copied",
    "policy.frozen_rules": "Frozen rules",
    "policy.v1_prohibitions": "V1 prohibitions",
    "policy.verdict_vocab": "Verdict vocabulary",
    "policy.duplicate_states": "Duplicate states",
    "policy.frozen_horizons": "Frozen horizons",
    "policy.hurdle": "Economic hurdle: {n} bps",
    "policy.quantiles": "Quantiles: {list}",
    "policy.engine_title": "FrozenAnalysisEngine",

    "audit.title": "System / Audit Log",
    "audit.subtitle":
      "Append-only ledger of every upload, verdict, retention decision and reprocessing. Rows are never edited or deleted.",
    "audit.events_count": "{n} events",
    "audit.search": "Search…",
    "audit.all_types": "All event types",
    "audit.all_outcomes": "All outcomes",
    "audit.refresh": "Refresh",
    "audit.empty": "No events yet.",

    "common.loading": "Loading…",
    "common.none": "—",
    "common.language_label": "Language",
  },
};

export function format(str, params) {
  if (!params) return str;
  return str.replace(/\{(\w+)\}/g, (_, k) => (params[k] != null ? params[k] : `{${k}}`));
}

export function getMessage(locale, key, params) {
  const bag = MESSAGES[locale] || MESSAGES[DEFAULT_LOCALE] || {};
  const fallback = MESSAGES[DEFAULT_LOCALE] || {};
  const raw = bag[key] != null ? bag[key] : fallback[key];
  if (raw == null) return key;
  return format(raw, params);
}

export function formatNumber(locale, value, opts) {
  try {
    return new Intl.NumberFormat(locale === "it" ? "it-IT" : "en-US", opts).format(value);
  } catch (_) {
    return String(value);
  }
}

export function formatDate(locale, iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return new Intl.DateTimeFormat(locale === "it" ? "it-IT" : "en-US", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).format(d);
  } catch (_) {
    return String(iso).replace("T", " ").slice(0, 19);
  }
}
