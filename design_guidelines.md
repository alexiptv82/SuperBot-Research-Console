{
  "meta": {
    "product": "SuperBot Research Console V1",
    "design_personality": [
      "institutional",
      "trustworthy",
      "scientifically precise",
      "calm",
      "data-first",
      "audit-friendly"
    ],
    "non_goals": [
      "No marketing hero sections",
      "No stock photography",
      "No flashy gradients",
      "No LLM-at-runtime vibes",
      "No consumer gamification"
    ],
    "ux_success_actions": [
      "Login",
      "Upload ZIP(s)",
      "Read verdict",
      "Approve next stage",
      "Export reports",
      "Audit trail review"
    ],
    "testing_requirement": {
      "rule": "All interactive and key informational elements MUST include data-testid",
      "convention": "kebab-case describing role (not appearance)",
      "examples": [
        "data-testid=\"login-form-submit-button\"",
        "data-testid=\"upload-dropzone\"",
        "data-testid=\"verdict-badge-pass\"",
        "data-testid=\"registry-table\"",
        "data-testid=\"audit-log-row\""
      ]
    }
  },
  "brand_attributes": {
    "tone": "Quiet authority. Reads like a lab instrument / compliance console.",
    "visual_metaphors": [
      "instrument panels",
      "research notebook",
      "immutable ledger",
      "terminal-grade density"
    ],
    "density_principle": "Dense but breathable: compact tables + generous section spacing + progressive disclosure for 30-field details."
  },
  "color_system": {
    "notes": [
      "Dark theme is default for long sessions; light theme is optional.",
      "Verdict colors are the emotional backbone and must be recognizable at ~2m distance.",
      "Avoid purple gradients; UNRESOLVED uses a solid indigo/violet token only (no gradients).",
      "Use semantic tokens; do not hardcode colors in components except for rare data viz accents."
    ],
    "tokens_css": {
      "path": "/app/frontend/src/index.css",
      "instructions": [
        "Replace the existing :root and .dark tokens with the following token set.",
        "Keep Tailwind/shadcn variable naming (HSL triplets).",
        "Do not introduce global gradients; keep backgrounds solid with subtle noise overlay (optional)."
      ],
      "css": "@layer base {\n  :root {\n    /* Light (optional) — paper-like, clinical */\n    --background: 210 20% 98%;\n    --foreground: 222 22% 12%;\n\n    --card: 0 0% 100%;\n    --card-foreground: 222 22% 12%;\n\n    --popover: 0 0% 100%;\n    --popover-foreground: 222 22% 12%;\n\n    /* Primary = ink / authority */\n    --primary: 222 22% 12%;\n    --primary-foreground: 210 20% 98%;\n\n    /* Secondary = subtle surface */\n    --secondary: 210 18% 94%;\n    --secondary-foreground: 222 22% 12%;\n\n    --muted: 210 18% 94%;\n    --muted-foreground: 215 12% 40%;\n\n    --accent: 210 18% 92%;\n    --accent-foreground: 222 22% 12%;\n\n    --border: 214 16% 86%;\n    --input: 214 16% 86%;\n    --ring: 215 20% 30%;\n\n    /* Destructive reserved for irreversible actions (not verdict) */\n    --destructive: 0 72% 46%;\n    --destructive-foreground: 210 20% 98%;\n\n    --radius: 0.6rem;\n\n    /* Console-specific semantic tokens */\n    --surface-0: 210 20% 98%;\n    --surface-1: 0 0% 100%;\n    --surface-2: 210 18% 96%;\n\n    --ink-1: 222 22% 12%;\n    --ink-2: 215 16% 28%;\n    --ink-3: 215 12% 40%;\n\n    /* Verdict tokens (light) */\n    --verdict-pass: 152 52% 34%;\n    --verdict-warn: 38 92% 42%;\n    --verdict-fail: 0 72% 42%;\n    --verdict-unresolved: 252 48% 44%;\n\n    /* Verdict backgrounds (light) */\n    --verdict-pass-bg: 152 52% 94%;\n    --verdict-warn-bg: 38 92% 94%;\n    --verdict-fail-bg: 0 72% 94%;\n    --verdict-unresolved-bg: 252 48% 94%;\n\n    /* Focus ring (slightly cool) */\n    --focus: 205 90% 40%;\n  }\n\n  .dark {\n    /* Dark (default) — terminal-grade, low glare */\n    --background: 220 18% 7%;\n    --foreground: 210 20% 96%;\n\n    --card: 220 18% 9%;\n    --card-foreground: 210 20% 96%;\n\n    --popover: 220 18% 9%;\n    --popover-foreground: 210 20% 96%;\n\n    /* Primary = light ink on dark */\n    --primary: 210 20% 96%;\n    --primary-foreground: 220 18% 9%;\n\n    --secondary: 220 14% 14%;\n    --secondary-foreground: 210 20% 96%;\n\n    --muted: 220 14% 14%;\n    --muted-foreground: 215 14% 70%;\n\n    --accent: 220 14% 16%;\n    --accent-foreground: 210 20% 96%;\n\n    --border: 220 12% 18%;\n    --input: 220 12% 18%;\n    --ring: 210 20% 80%;\n\n    --destructive: 0 62% 34%;\n    --destructive-foreground: 210 20% 96%;\n\n    --radius: 0.6rem;\n\n    /* Console surfaces */\n    --surface-0: 220 18% 7%;\n    --surface-1: 220 18% 9%;\n    --surface-2: 220 14% 12%;\n\n    --ink-1: 210 20% 96%;\n    --ink-2: 215 14% 78%;\n    --ink-3: 215 12% 64%;\n\n    /* Verdict tokens (dark) — tuned for AA contrast on dark */\n    --verdict-pass: 152 58% 52%;\n    --verdict-warn: 38 92% 56%;\n    --verdict-fail: 0 78% 58%;\n    --verdict-unresolved: 252 62% 70%;\n\n    /* Verdict backgrounds (dark) */\n    --verdict-pass-bg: 152 40% 14%;\n    --verdict-warn-bg: 38 55% 14%;\n    --verdict-fail-bg: 0 55% 14%;\n    --verdict-unresolved-bg: 252 40% 16%;\n\n    --focus: 205 90% 55%;\n  }\n}\n\n@layer base {\n  ::selection {\n    background: hsl(var(--focus) / 0.25);\n  }\n}\n"
    },
    "semantic_usage": {
      "verdict": {
        "PASS": {
          "fg": "hsl(var(--verdict-pass))",
          "bg": "hsl(var(--verdict-pass-bg))",
          "border": "hsl(var(--verdict-pass) / 0.35)"
        },
        "PASS_WITH_WARNING": {
          "fg": "hsl(var(--verdict-warn))",
          "bg": "hsl(var(--verdict-warn-bg))",
          "border": "hsl(var(--verdict-warn) / 0.35)"
        },
        "FAIL": {
          "fg": "hsl(var(--verdict-fail))",
          "bg": "hsl(var(--verdict-fail-bg))",
          "border": "hsl(var(--verdict-fail) / 0.35)"
        },
        "UNRESOLVED": {
          "fg": "hsl(var(--verdict-unresolved))",
          "bg": "hsl(var(--verdict-unresolved-bg))",
          "border": "hsl(var(--verdict-unresolved) / 0.35)"
        }
      },
      "status_pills": {
        "FrozenAnalysisEngine": {
          "NOT_CONFIGURED": {
            "fg": "hsl(var(--ink-2))",
            "bg": "hsl(var(--muted))",
            "border": "hsl(var(--border))"
          }
        }
      }
    },
    "gradients": {
      "policy": "No gradients by default. If absolutely needed, only use a very mild 2-color background wash in hero-like header areas and keep it under 20% viewport. This console should remain mostly solid surfaces.",
      "allowed_example": "background: radial-gradient(1200px circle at 20% 0%, hsl(205 90% 55% / 0.08), transparent 55%);",
      "prohibited": [
        "blue-500 to purple-600",
        "purple-500 to pink-500",
        "green-500 to blue-500",
        "red to pink"
      ]
    }
  },
  "typography": {
    "font_pairing": {
      "ui_sans": {
        "preferred": "Inter",
        "fallback": "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
        "usage": "Navigation, headings, labels, buttons"
      },
      "mono": {
        "preferred": "JetBrains Mono",
        "fallback": "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, monospace",
        "usage": "Hashes, IDs, timestamps, numeric tables, code-like policy blocks"
      }
    },
    "google_fonts_import": {
      "instructions": "Add to /app/frontend/public/index.html <head> (or equivalent) using Google Fonts. Keep display=swap.",
      "href": "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap"
    },
    "scale_tailwind": {
      "h1": "text-4xl sm:text-5xl lg:text-6xl font-semibold tracking-tight",
      "h2": "text-base md:text-lg font-medium text-muted-foreground",
      "section_title": "text-sm font-semibold tracking-wide text-foreground",
      "body": "text-sm md:text-base text-foreground",
      "small": "text-xs text-muted-foreground",
      "mono": "font-mono text-xs md:text-sm tabular-nums"
    },
    "numeric_rules": [
      "Use tabular-nums for all numeric KPIs and table cells.",
      "Use font-mono for hashes, session_id, collector SHA256, timestamps.",
      "Avoid ALL CAPS paragraphs; reserve caps for tiny labels/badges only."
    ]
  },
  "layout_and_grid": {
    "app_shell": {
      "pattern": "Sidebar + top bar + content",
      "sidebar_width": "w-[264px] (desktop), collapsible to icon rail; mobile uses Sheet",
      "content_max_width": "max-w-[1400px] for readability; do not center-align text globally",
      "page_padding": "px-4 sm:px-6 lg:px-8 py-6",
      "section_spacing": "space-y-6 (cards), space-y-3 (dense groups)"
    },
    "overview_grid": {
      "desktop": "grid grid-cols-12 gap-4",
      "cards": {
        "checkpoint_cluster": "col-span-12 lg:col-span-7",
        "recent_uploads": "col-span-12 lg:col-span-5",
        "engine_status": "col-span-12 lg:col-span-5",
        "quick_actions": "col-span-12 lg:col-span-7"
      }
    },
    "registry_grid": {
      "table_container": "Card with sticky header + ScrollArea; keep filters in a compact toolbar"
    }
  },
  "spacing_radius_shadows": {
    "spacing": {
      "micro": "gap-2, space-y-2",
      "standard": "gap-3/4, space-y-3/4",
      "section": "space-y-6",
      "table_density": "py-2 px-3 (compact), py-3 px-4 (comfortable)"
    },
    "radius": {
      "global": "--radius: 0.6rem",
      "badges": "rounded-md",
      "buttons": "rounded-md",
      "cards": "rounded-xl (Card root), inner panels rounded-lg"
    },
    "shadows": {
      "policy": "Minimal elevation; rely on borders and subtle shadow only for popovers/dialogs.",
      "tokens": {
        "card": "shadow-[0_1px_0_hsl(var(--border))]",
        "popover": "shadow-lg shadow-black/20 (dark)"
      }
    }
  },
  "components": {
    "component_path": {
      "shadcn_primary": "/app/frontend/src/components/ui/",
      "use_these": [
        "button.jsx",
        "badge.jsx",
        "card.jsx",
        "table.jsx",
        "tabs.jsx",
        "dialog.jsx",
        "alert.jsx",
        "progress.jsx",
        "scroll-area.jsx",
        "sheet.jsx",
        "separator.jsx",
        "tooltip.jsx",
        "sonner.jsx",
        "input.jsx",
        "label.jsx",
        "switch.jsx",
        "dropdown-menu.jsx",
        "command.jsx",
        "collapsible.jsx",
        "accordion.jsx"
      ]
    },
    "top_bar": {
      "structure": [
        "Left: breadcrumb + page title",
        "Right: session count, FrozenAnalysisEngine status pill, theme toggle, user/logout"
      ],
      "classes": "h-14 border-b bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60",
      "data_testids": [
        "topbar-session-count",
        "topbar-engine-status-pill",
        "topbar-theme-toggle",
        "topbar-logout-button"
      ]
    },
    "sidebar_nav": {
      "pattern": "Icon + label, active indicator bar, compact spacing",
      "classes": "border-r bg-card",
      "active_state": "bg-accent text-foreground before:absolute before:left-0 before:top-2 before:bottom-2 before:w-[2px] before:bg-[hsl(var(--focus))]",
      "data_testids": [
        "sidebar-nav-overview",
        "sidebar-nav-upload",
        "sidebar-nav-registry",
        "sidebar-nav-reports",
        "sidebar-nav-checkpoints",
        "sidebar-nav-policy",
        "sidebar-nav-audit"
      ]
    },
    "verdict_badge": {
      "goal": "Instantly identifiable at distance; consistent everywhere.",
      "base": "inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs font-semibold tracking-wide",
      "dot": "h-2 w-2 rounded-full",
      "variants": {
        "PASS": {
          "badge": "border-[hsl(var(--verdict-pass)/0.35)] bg-[hsl(var(--verdict-pass-bg))] text-[hsl(var(--verdict-pass))]",
          "dot": "bg-[hsl(var(--verdict-pass))]"
        },
        "PASS_WITH_WARNING": {
          "badge": "border-[hsl(var(--verdict-warn)/0.35)] bg-[hsl(var(--verdict-warn-bg))] text-[hsl(var(--verdict-warn))]",
          "dot": "bg-[hsl(var(--verdict-warn))]"
        },
        "FAIL": {
          "badge": "border-[hsl(var(--verdict-fail)/0.35)] bg-[hsl(var(--verdict-fail-bg))] text-[hsl(var(--verdict-fail))]",
          "dot": "bg-[hsl(var(--verdict-fail))]"
        },
        "UNRESOLVED": {
          "badge": "border-[hsl(var(--verdict-unresolved)/0.35)] bg-[hsl(var(--verdict-unresolved-bg))] text-[hsl(var(--verdict-unresolved))]",
          "dot": "bg-[hsl(var(--verdict-unresolved))]"
        }
      },
      "placement_rules": [
        "Always show verdict badge in: upload rows, registry table, session header, audit log rows.",
        "Never rely on color alone: include label text + dot.",
        "Badge must be readable at 12px; do not shrink below text-xs."
      ],
      "data_testids": [
        "verdict-badge",
        "verdict-badge-pass",
        "verdict-badge-pass-with-warning",
        "verdict-badge-fail",
        "verdict-badge-unresolved"
      ]
    },
    "checkpoint_progress": {
      "pattern": "Rings for quick glance + bars for exactness",
      "ring": {
        "implementation": "Use SVG ring component (custom) inside Card; keep stroke width 8–10; show numeric center label in mono.",
        "classes": "relative grid place-items-center",
        "center": "font-mono tabular-nums text-sm",
        "caption": "text-xs text-muted-foreground",
        "colors": {
          "track": "hsl(var(--border))",
          "fill": "hsl(var(--focus))"
        },
        "data_testids": [
          "checkpoint-ring-old36",
          "checkpoint-ring-new12",
          "checkpoint-ring-total48",
          "checkpoint-ring-new36",
          "checkpoint-ring-total72"
        ]
      },
      "bar": {
        "component": "Use shadcn Progress (/components/ui/progress.jsx) for linear bars.",
        "classes": "h-2 rounded-full",
        "label_row": "flex items-baseline justify-between gap-3",
        "data_testids": [
          "checkpoint-bar-old36",
          "checkpoint-bar-new12",
          "checkpoint-bar-total48",
          "checkpoint-bar-new36",
          "checkpoint-bar-total72"
        ]
      },
      "readiness_flag": {
        "pattern": "A single, high-salience pill: 72H_DATA_QA_READY",
        "classes": "inline-flex items-center rounded-md border px-2 py-1 text-xs font-semibold",
        "states": {
          "true": "border-[hsl(var(--verdict-pass)/0.35)] bg-[hsl(var(--verdict-pass-bg))] text-[hsl(var(--verdict-pass))]",
          "false": "border-border bg-muted text-muted-foreground"
        },
        "data_testids": [
          "checkpoints-ready-flag"
        ]
      }
    },
    "dense_tables_registry": {
      "component": "Use shadcn Table (/components/ui/table.jsx) inside Card + ScrollArea.",
      "density": {
        "default": "compact",
        "row": "text-xs md:text-sm",
        "cell": "px-3 py-2 align-middle",
        "header": "sticky top-0 bg-card/95 backdrop-blur border-b",
        "mono_columns": [
          "session_id",
          "sha256",
          "timestamps",
          "counts"
        ]
      },
      "table_toolbar": {
        "pattern": "Single-line toolbar: search + status filter + date range + density toggle",
        "components": [
          "Input",
          "DropdownMenu or Select",
          "Calendar (if date range)",
          "ToggleGroup (density)"
        ],
        "classes": "flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between",
        "data_testids": [
          "registry-search-input",
          "registry-status-filter",
          "registry-density-toggle",
          "registry-table"
        ]
      },
      "row_interactions": {
        "hover": "hover:bg-accent/60",
        "focus": "focus-within:ring-2 focus-within:ring-[hsl(var(--focus))]",
        "click": "Row click navigates to /session/:id; also provide explicit View button for accessibility.",
        "data_testids": [
          "registry-row",
          "registry-row-view-button"
        ]
      }
    },
    "session_detail_progressive_disclosure": {
      "pattern": "Grouped sections with Collapsible/Accordion; summary header always visible.",
      "groups": [
        "Identity",
        "Runtime",
        "Dataset Structure",
        "Parquet Integrity",
        "Reconnects",
        "Verdict",
        "Retention",
        "Reprocess History"
      ],
      "components": [
        "Accordion",
        "Collapsible",
        "Separator",
        "Badge",
        "Table (for key/value pairs)",
        "Tooltip (for field definitions)"
      ],
      "key_value_layout": {
        "pattern": "2-column definition grid",
        "classes": "grid grid-cols-1 md:grid-cols-[220px_1fr] gap-x-6 gap-y-2",
        "label": "text-xs font-medium text-muted-foreground",
        "value": "font-mono text-xs md:text-sm text-foreground break-all"
      },
      "data_testids": [
        "session-detail-header",
        "session-detail-download-json",
        "session-detail-download-csv",
        "session-detail-download-md",
        "session-detail-reprocess-button",
        "session-detail-section"
      ]
    },
    "upload_dropzone": {
      "pattern": "Dark dropzone with clear states + per-file rows with progress + verdict badge",
      "dropzone": {
        "classes": "rounded-xl border border-dashed bg-card p-6 sm:p-8 transition-colors",
        "idle": "border-border",
        "drag_active": "border-[hsl(var(--focus))] bg-[hsl(var(--focus)/0.06)]",
        "error": "border-[hsl(var(--verdict-fail))] bg-[hsl(var(--verdict-fail-bg))]",
        "helper_text": "text-sm text-muted-foreground",
        "data_testids": [
          "upload-dropzone",
          "upload-file-input"
        ]
      },
      "file_row": {
        "classes": "flex items-center gap-3 rounded-lg border bg-background/40 px-3 py-2",
        "left": "file icon + name + size",
        "right": "progress + verdict badge + actions",
        "progress": "Use shadcn Progress with h-1.5",
        "actions": [
          "Remove",
          "Retry"
        ],
        "data_testids": [
          "upload-file-row",
          "upload-file-progress",
          "upload-file-remove-button",
          "upload-file-retry-button"
        ]
      },
      "retain_raw_toggle": {
        "component": "Switch",
        "copy": "Retain raw ZIP after validation",
        "data_testids": [
          "upload-retain-raw-toggle"
        ]
      }
    },
    "audit_log_append_only": {
      "pattern": "Immutable ledger feel: timestamp + actor + action + object + outcome",
      "container": "Card + ScrollArea; newest at top with subtle divider",
      "row": {
        "classes": "grid grid-cols-[140px_1fr] sm:grid-cols-[160px_140px_1fr_auto] gap-3 px-3 py-2 border-b",
        "timestamp": "font-mono text-xs text-muted-foreground",
        "actor": "text-xs font-medium",
        "message": "text-xs md:text-sm",
        "outcome": "verdict badge or neutral pill",
        "hover": "hover:bg-accent/50",
        "data_testids": [
          "audit-log",
          "audit-log-row",
          "audit-log-timestamp",
          "audit-log-action",
          "audit-log-outcome"
        ]
      },
      "filters": {
        "pattern": "Compact filters: event type, verdict, date range",
        "data_testids": [
          "audit-filter-event-type",
          "audit-filter-verdict",
          "audit-filter-date-range"
        ]
      }
    },
    "policy_page": {
      "pattern": "Verbatim rules in a readable, code-like block with copy buttons",
      "sha256_callout": {
        "classes": "rounded-lg border bg-[hsl(var(--surface-2))] p-4",
        "hash": "font-mono text-xs break-all",
        "copy_button": "Button variant=secondary size=sm",
        "data_testids": [
          "policy-collector-sha256",
          "policy-copy-sha256-button"
        ]
      },
      "rules_block": {
        "classes": "rounded-lg border bg-card p-4 font-mono text-xs leading-relaxed",
        "data_testids": [
          "policy-rules-block"
        ]
      }
    },
    "login_screen": {
      "pattern": "Single-purpose gate; no branding fluff",
      "layout": "Centered card but left-aligned text inside; subtle security copy",
      "card": "max-w-md w-full rounded-xl border bg-card p-6",
      "fields": {
        "password": "Input type=password with show/hide toggle",
        "submit": "Primary button full width"
      },
      "data_testids": [
        "login-password-input",
        "login-submit-button",
        "login-error-message"
      ]
    },
    "reports_page": {
      "pattern": "Export tools as cards with clear formats; show last generated timestamp",
      "buttons": {
        "json": "Button variant=secondary",
        "csv": "Button variant=secondary",
        "md": "Button variant=secondary"
      },
      "data_testids": [
        "reports-export-json-button",
        "reports-export-csv-button",
        "reports-export-md-button"
      ]
    },
    "engine_status_card": {
      "pattern": "FrozenAnalysisEngine status pill + explanation + disabled CTA",
      "status": {
        "pill": "Badge-like neutral pill",
        "copy": "Frozen 72H analysis is NOT_CONFIGURED in V1.",
        "cta": "Button disabled with tooltip explaining future feature"
      },
      "data_testids": [
        "engine-status-card",
        "engine-status-pill",
        "engine-status-cta"
      ]
    }
  },
  "buttons": {
    "style": "Professional / Corporate",
    "variants": {
      "primary": {
        "usage": "Approve next stage, Login, Start validation",
        "classes": "bg-primary text-primary-foreground hover:bg-primary/90",
        "motion": "transition-colors duration-150"
      },
      "secondary": {
        "usage": "Download, Copy, Filters",
        "classes": "bg-secondary text-secondary-foreground hover:bg-secondary/80",
        "motion": "transition-colors duration-150"
      },
      "ghost": {
        "usage": "Row actions, icon buttons",
        "classes": "hover:bg-accent hover:text-accent-foreground",
        "motion": "transition-colors duration-150"
      },
      "destructive": {
        "usage": "Remove upload, irreversible actions",
        "classes": "bg-destructive text-destructive-foreground hover:bg-destructive/90",
        "motion": "transition-colors duration-150"
      }
    },
    "interaction": {
      "press": "active:translate-y-[0.5px] active:opacity-95",
      "focus": "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--focus))] focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    },
    "data_testids": [
      "primary-action-button",
      "secondary-action-button"
    ]
  },
  "motion_and_microinteractions": {
    "principles": [
      "No gimmicks. Motion is functional: state change clarity, focus guidance.",
      "Prefer subtle opacity/color transitions; avoid large transforms.",
      "Respect prefers-reduced-motion."
    ],
    "allowed": {
      "hover": "transition-colors duration-150",
      "collapse": "Accordion/Collapsible default animations are acceptable",
      "loading": "Skeleton for tables and detail sections"
    },
    "avoid": [
      "No parallax",
      "No bouncing",
      "No looping decorative animations",
      "No universal transition: all"
    ]
  },
  "accessibility": {
    "contrast": [
      "All text must meet WCAG AA against background.",
      "Verdict badges must include text label + dot; do not rely on color alone.",
      "Focus ring must be visible on dark and light themes (use --focus token)."
    ],
    "keyboard": [
      "All table row actions must be reachable via keyboard.",
      "Provide explicit View buttons/links in tables (not only row click)."
    ],
    "aria": [
      "Add aria-label to icon-only buttons.",
      "Use aria-live polite region for upload progress and verdict updates."
    ]
  },
  "data_visualization": {
    "policy": "Keep charts minimal. Prefer progress rings/bars and small sparklines only if needed.",
    "optional_library": {
      "name": "recharts",
      "use_cases": [
        "Tiny sparkline for validated hours trend",
        "Histogram of verdict counts"
      ],
      "install": "npm i recharts",
      "notes": "Use muted strokes; no gradients; keep axes minimal; use mono tick labels."
    }
  },
  "images": {
    "policy": "No stock photography. Use only subtle procedural noise texture (CSS) if desired.",
    "image_urls": []
  },
  "implementation_notes_js": {
    "rule": "Project uses .js (not .tsx). Keep components in JS and use PropTypes only if already used in repo.",
    "suggested_utilities": {
      "cn": "Use existing shadcn cn utility if present; otherwise keep className strings simple.",
      "theme_toggle": "Use 'dark' class on html/body root; persist in localStorage."
    }
  },
  "instructions_to_main_agent": [
    "Update /app/frontend/src/App.css to remove the default CRA centered header styles; do not center the app container globally.",
    "Replace token set in /app/frontend/src/index.css with the provided tokens_css.css block.",
    "Implement VerdictBadge as a small reusable component using /components/ui/badge.jsx styling + custom classes; ensure data-testid is applied.",
    "Implement CheckpointRing as a small SVG component (JS) and use shadcn Progress for bars.",
    "Use shadcn Table + ScrollArea for registry and audit log; keep compact density (px-3 py-2).",
    "Ensure every button/input/filter/table row/action has a stable data-testid.",
    "Keep motion minimal and functional; no parallax; no gradients except optional tiny header wash under 20% viewport."
  ],
  "append_general_ui_ux_design_guidelines": "<General UI UX Design Guidelines>  \n    - You must **not** apply universal transition. Eg: `transition: all`. This results in breaking transforms. Always add transitions for specific interactive elements like button, input excluding transforms\n    - You must **not** center align the app container, ie do not add `.App { text-align: center; }` in the css file. This disrupts the human natural reading flow of text\n   - NEVER: use AI assistant Emoji characters like`🤖🧠💭💡🔮🎯📚🎭🎬🎪🎉🎊🎁🎀🎂🍰🎈🎨🎰💰💵💳🏦💎🪙💸🤑📊📈📉💹🔢🏆🥇 etc for icons. Always use **FontAwesome cdn** or **lucid-react** library already installed in the package.json\n\n **GRADIENT RESTRICTION RULE**\nNEVER use dark/saturated gradient combos (e.g., purple/pink) on any UI element.  Prohibited gradients: blue-500 to purple 600, purple 500 to pink-500, green-500 to blue-500, red to pink etc\nNEVER use dark gradients for logo, testimonial, footer etc\nNEVER let gradients cover more than 20% of the viewport.\nNEVER apply gradients to text-heavy content or reading areas.\nNEVER use gradients on small UI elements (<100px width).\nNEVER stack multiple gradient layers in the same viewport.\n\n**ENFORCEMENT RULE:**\n    • Id gradient area exceeds 20% of viewport OR affects readability, **THEN** use solid colors\n\n**How and where to use:**\n   • Section backgrounds (not content backgrounds)\n   • Hero section header content. Eg: dark to light to dark color\n   • Decorative overlays and accent elements only\n   • Hero section with 2-3 mild color\n   • Gradients creation can be done for any angle say horizontal, vertical or diagonal\n\n- For AI chat, voice application, **do not use purple color. Use color like light green, ocean blue, peach orange etc**\n\n</Font Guidelines>\n\n- Every interaction needs micro-animations - hover states, transitions, parallax effects, and entrance animations. Static = dead. \n   \n- Use 2-3x more spacing than feels comfortable. Cramped designs look cheap.\n\n- Subtle grain textures, noise overlays, custom cursors, selection states, and loading animations: separates good from extraordinary.\n   \n- Before generating UI, infer the visual style from the problem statement (palette, contrast, mood, motion) and immediately instantiate it by setting global design tokens (primary, secondary/accent, background, foreground, ring, state colors), rather than relying on any library defaults. Don't make the background dark as a default step, always understand problem first and define colors accordingly\n    Eg: - if it implies playful/energetic, choose a colorful scheme\n           - if it implies monochrome/minimal, choose a black–white/neutral scheme\n\n**Component Reuse:**\n\t- Prioritize using pre-existing components from src/components/ui when applicable\n\t- Create new components that match the style and conventions of existing components when needed\n\t- Examine existing components to understand the project's component patterns before creating new ones\n\n**IMPORTANT**: Do not use HTML based component like dropdown, calendar, toast etc. You **MUST** always use `/app/frontend/src/components/ui/ ` only as a primary components as these are modern and stylish component\n\n**Best Practices:**\n\t- Use Shadcn/UI as the primary component library for consistency and accessibility\n\t- Import path: ./components/[component-name]\n\n**Export Conventions:**\n\t- Components MUST use named exports (export const ComponentName = ...)\n\t- Pages MUST use default exports (export default function PageName() {...})\n\n**Toasts:**\n  - Use `sonner` for toasts\"\n  - Sonner component are located in `/app/src/components/ui/sonner.tsx`\n\nUse 2–4 color gradients, subtle textures/noise overlays, or CSS-based noise to avoid flat visuals.\n</General UI UX Design Guidelines>"
}
