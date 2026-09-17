# /app/recovered — MultiVenue V2 Validator Source Recovery Manifest

Generated: 2026-09-17 UTC  
Scope: forensic inventory of every artefact currently present in the SuperBot
project that is materially related to the MultiVenue V2 CP24 / CP36
quantitative-analysis pipeline. No reconstruction was performed; every file
below is copied verbatim from its origin.

| # | file | origin | sha256 | comment |
|---|------|--------|--------|---------|
| 00 | `00-frozen_collector.py` | `/app/audit/collector.py` | `e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3` | Bitget Multi-Venue Microstructure **COLLECTOR** V2 — 43 947 B. Emits raw normalized event stream + synchronized 100 ms grid + Parquet output. **This is NOT the validator/analysis script.** Contains ZERO quantile/composite/overlap/forward-return logic. |
| 01 | `01-FROZEN_ENGINE_RECOVERY_AUDIT.md` | `/app/audit/FROZEN_ENGINE_RECOVERY_AUDIT.md` | `1a42fbb6aa428a968d73a8eaf7089abc6d2ce63311b314d199af6de7e0edd983` | Frozen-engine recovery audit report (21 969 B). Explicitly enumerates what is present in CP24 + CP36 ZIPs and what is **NOT** — see §51 for the definitive "not present" list. |
| 02 | `02-phase1_audit.md` | `/app/backend/recovery/goldens/phase1_audit.md` | `1a42fbb6aa428a968d73a8eaf7089abc6d2ce63311b314d199af6de7e0edd983` | **Identical byte-for-byte to file #01** (same sha256). Duplicated copy under the recovery goldens tree. |
| 03 | `03-recovery_rules_catalog.py` | `/app/backend/recovery/rules.py` | `cdb3cf331fd532a14bac0018965c5b67d4bde76cbe2e822f81665574a89f7992` | Structured RULE catalogue (16 087 B). Each rule tagged `EXPLICIT_EVIDENCE`, `MATH_UNIQUE`, `INFERRED_PENDING`, `AMBIGUOUS`, or `MISSING`. Contains rule METADATA and provenance pointers only — never the actual composite formulas. |
| 04 | `04-recovery_rules.json` | `/app/backend/recovery/reports/recovery_rules.json` | `e8d1dbfc91dc3093a2c971b8e6ca18075ad2c66030cf042143a11a317cc5d2cb` | Machine-readable snapshot of the rule catalogue (13 779 B). |
| 05 | `05-recovery_provenance.json` | `/app/backend/recovery/reports/recovery_provenance.json` | `55bde9c6c90b6fc8a16240f9d5427a1b836872b38ed65259492d405e159b152f` | Per-rule provenance / distinguishing evidence catalogue (10 796 B). |
| 06 | `06-recovery_report.md` | `/app/backend/recovery/reports/recovery_report.md` | `c6dfd296f72b6226a5aae52d432ccbe204373ad4a6d03ea5484702ca72c2691d` | Latest recovery-audit run summary (1 622 B) — reports `raw_ready: false`, `Pending: 11449` for goldens. |
| 07 | `07-CHECKPOINT_24H_REPORT.md` | `/app/audit/cp24_x/MULTIVENUE_V2_CHECKPOINT_24H/CHECKPOINT_24H_REPORT.md` | `bcc948f4de2a05b8c852ff34ddc976bd4e86029dac5f442dfd2d1d507374b368` | The historical CP24 written report (5 340 B). Prose only — no code / no formulas. |
| 08 | `08-CHECKPOINT_36H_REPORT.md` | `/app/audit/cp36_x/MULTIVENUE_V2_CHECKPOINT_36H/CHECKPOINT_36H_REPORT.md` | `b461153777f774ea4d012b620fbaef67da0fcc2580f5529454ee416f9071912f` | The historical CP36 written report (8 550 B). Prose only — no code / no formulas. |

## Master finding

Across the ENTIRE filesystem (root `/`, project `/app`, `/app/.emergent`, `/tmp`, `/content`, `/workspace`, every git branch, every git stash, every deleted blob in git history) there is **no** Jupyter notebook, no `validator*.py`, no `analyze*.py`, no `multivenue*.py`, no `block_level*` script, no `cp24*.py` / `cp36*.py`, no YAML/JSON containing composite weights, no per-feature normalisation table, no BTC/ETH aggregation weights file, no overlap-ordering / tie / NaN / end-of-session policy source, no OLD24 or NEW12 raw parquet grid.

The `FROZEN_ENGINE_RECOVERY_AUDIT.md` at line 51 states this explicitly:

> "**Not present in either ZIP:** any Python source, Jupyter notebook, YAML/JSON configuration, composite-weight file, forward-return construction script, event-selection algorithm, dispersion-tertile boundary computation, BTC/ETH aggregation weights, or the OLD24/NEW12 raw parquet grids themselves."

This is corroborated by every filesystem search below. **The recovery corpus copied to `/app/recovered/` above is genuinely everything that exists.**
