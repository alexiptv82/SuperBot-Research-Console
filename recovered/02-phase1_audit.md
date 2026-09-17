# FROZEN ANALYSIS ENGINE — READ-ONLY RECOVERY AUDIT

**Date:** 2026-09-15
**Scope:** determine whether the EXACT frozen quantitative validator used for the historical 24H / 36H checkpoints can be recovered from the four supplied artifacts without inference or reconstruction from prose.
**Status:** READ-ONLY. No code was changed. `FrozenAnalysisEngine` remains `NOT_CONFIGURED`. Directional alpha remains **REJECTED**. Live trading remains **NOT AUTHORIZED**.

---

## 1. Complete inventory of the checkpoint ZIPs

### 1.1 `Bitget_MultiVenue_V2_CHECKPOINT_24H_RESULTS.zip`

Root: `MULTIVENUE_V2_CHECKPOINT_24H/`

| File | Size (B) | Content |
|---|---:|---|
| `CHECKPOINT_24H_REPORT.md` | 5 340 | Human-readable verdict, methodology narration, promotion decisions |
| `session_audit_24h.csv` | 2 011 | 7 rows — operational QA per session (grid_files, book_files, trade_files, missed_ticks, ws_errors, reconnects, collector_sha256) |
| `simple_q80_q90_q95_block_level_24h.csv` | 591 529 | **4 032 rows** — per-(session,asset,feature,horizon,q) — 14 blocks × 12 simple features × 8 horizons × 3 q. Columns: `session,period,asset,feature,horizon_ms,q,threshold,N,mean_signed_bps,median_signed_bps,hit_rate,mean_abs_move` |
| `composite_q80_q90_q95_block_level_24h.csv` | 248 349 | **3 360 rows** — per-(session,asset,feature,horizon,q) — 14 blocks × 10 composites × 8 horizons × 3 q. Columns: `session,period,asset,feature,horizon_ms,q,threshold,N,mean_signed_bps,hit_rate` |
| `simple_aggregates_24h.csv` | 111 429 | Feature-level aggregates over 6 (WEEKEND12) / 8 (WEEKDAY12) / 14 (ALL24) blocks × 12 features × 8 horizons × 3 q. Adds `blocks, positive_blocks, positive_share, min_block, max_block, avg_hit_rate, period` |
| `composite_aggregates_24h.csv` | 55 573 | Same shape for 10 composite features |
| `fair_gap_dispersion_block_level_24h.csv` | 18 011 | Per-(session,asset,state ∈ {low,mid,high},horizon) fair-gap event effect by external dispersion tertile. Includes `disp_lo, disp_hi, threshold` |
| `fair_gap_dispersion_aggregates_24h.csv` | 3 109 | Same aggregated over WEEKEND12 / WEEKDAY12 / ALL24 |
| `weekend_vs_weekday_selected_30s.csv` | 2 821 | Weekend vs weekday selected q90/30s comparison (11 selected features, both simple and composite) |

### 1.2 `Bitget_MultiVenue_V2_CHECKPOINT_36H_RESULTS.zip`

Root: `MULTIVENUE_V2_CHECKPOINT_36H/`

| File | Size (B) | Content |
|---|---:|---|
| `CHECKPOINT_36H_REPORT.md` | 8 550 | 36H verdict, methodology narration, decisions |
| `session_audit_new12.csv` | 2 521 | 4 rows — operational QA for the NEW 12h batch only |
| `session_audit_36h.csv` | 3 469 | 11 rows — operational QA for the union of 24H (7) + NEW12 (4) |
| `simple_block_level_new12.csv` | 347 395 | **2 304 rows** — 8 blocks × 12 features × 8 horizons × 3 q, same schema as 24H |
| `composite_block_level_new12.csv` | 145 096 | **1 920 rows** — 8 blocks × 10 composites × 8 horizons × 3 q, same schema as 24H |
| `simple_horizon_profile_new12_q90.csv` | 5 559 | Per-feature horizon profile aggregated across the 8 new12 blocks (q=0.9 only) |
| `simple_horizon_profile_all36_q90.csv` | 5 652 | Per-feature horizon profile aggregated across all 22 blocks (q=0.9 only) |
| `composite_horizon_profile_new12_q90.csv` | 2 804 | Composite horizon profile new12 |
| `composite_horizon_profile_all36_q90.csv` | 2 852 | Composite horizon profile all-36H |
| `simple_sensitivity_new12_30s.csv` | 2 114 | q80/q90/q95 sensitivity at 30s per simple feature (new12) |
| `simple_sensitivity_all36_30s.csv` | 2 234 | q80/q90/q95 sensitivity at 30s per simple feature (all-36H) |
| `composite_sensitivity_new12_30s.csv` | 1 682 | q80/q90/q95 sensitivity at 30s per composite (new12) |
| `composite_sensitivity_all36_30s.csv` | 1 720 | q80/q90/q95 sensitivity at 30s per composite (all-36H) |
| `fair_gap_dispersion_block_level_new12.csv` | 10 576 | Per-block dispersion-tertile fair-gap analysis (new12) |
| `fair_gap_dispersion_new12.csv` | 990 | Aggregated new12 |
| `fair_gap_dispersion_all36.csv` | 1 050 | Aggregated all-36H |
| `selected_q90_30s_comparison_24h_new12_all36.csv` | 1 253 | Direct comparison across the 24H, NEW12, and ALL36 windows for the selected q90/30s features |

**Not present in either ZIP:** any Python source, Jupyter notebook, YAML/JSON configuration, composite-weight file, forward-return construction script, event-selection algorithm, dispersion-tertile boundary computation, BTC/ETH aggregation weights, or the OLD24/NEW12 raw parquet grids themselves.

---

## 2. Exact frozen-collector fields (relevant to analysis)

Source: `Bitget_MultiVenue_Microstructure_Collector_V2.py` (SHA256 `e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3`, immutable per project directive).

**Identifiers and clocks (per grid row):**
- `session_id`, `asset` ∈ {BTC, ETH}
- `local_ts_ms`, `sample_monotonic_ns`, `sample_interval_ms`, `sampler_lag_ms`, `sampler_missed_ticks`

**Per-venue snapshot fields (prefix `{venue}_`, one for each of `bitget`, `binance`, `okx`, `bybit`):**
- `mid`, `bid`, `bid_qty`, `ask`, `ask_qty`
- `bid_depth_l5`, `ask_depth_l5`, `depth_imbalance_l5`
- `microprice_l1`, `spread_bps`
- `book_age_recv_ms`, `book_age_exchange_ms`, `book_latency_wall_ms`
- `fresh` (bool, true iff `book_age_recv_ms <= FRESH_MS[venue]`)
- `depth_imbalance_l1`, `ofi_raw_window`, `ofi_norm_l1`, `book_count_window`
- `trade_imbalance_window`, `trade_count_window`
- `last_trade_price`, `last_trade_age_ms`
- `raw_basis_bps`, `basis_ewma_prior_bps`, `adjusted_mid` (external venues only)

**Top-level (cross-venue, one per grid row):**
- `external_perp_fresh_count`, `external_perp_dispersion_bps`
- `adjusted_fair_venue_count` (**quality filter driver**, must be ≥ 2 per handoff §12)
- `fair_price_equal_geo` (basis-adjusted equal-weight **geometric** mean of adjusted external mids; ≥ 2 fresh)
- `fair_price_adjusted_median` (challenger)
- `bitget_gap_to_fair_bps` = `(bitget_mid / fair_price_equal_geo − 1) × 10 000`
- `external_trade_imbalance_consensus`, `external_ofi_consensus_l1`
- `external_trade_active_venues`, `external_ofi_active_venues`
- Multi-horizon leader impulses (100 / 200 / 500 / 1000 ms): `fair_ret_{ms}ms_bps`, `bitget_ret_{ms}ms_bps`, `leader_gap_{ms}ms_bps` (= `fair_ret − bg_ret`)
- `fair_accel_100ms_bps` = `fair_ret_100ms_bps − prev(fair_ret_100ms_bps)`
- `bitget_fair_ofi_alignment` = `sign(−gap) × sign(bitget_ofi_norm_l1)` when both finite and non-zero, else NaN
- `bitget_fair_trade_alignment` = `sign(−gap) × sign(bitget_trade_imbalance_window)` (same rules)

**Direct mapping to analysis feature names used in the checkpoint CSVs** (recovered by inspecting column values, thresholds and the collector code together):

| Analysis feature | Grid column | Direction rule |
|---|---|---|
| `bitget_ofi` | `bitget_ofi_norm_l1` | `+sign(signal)` |
| `bitget_trade_flow` | `bitget_trade_imbalance_window` | `+sign(signal)` |
| `depth_imbalance_l1` | `bitget_depth_imbalance_l1` | `+sign(signal)` |
| `depth_imbalance_l5` | `bitget_depth_imbalance_l5` | `+sign(signal)` |
| `external_ofi` | `external_ofi_consensus_l1` | `+sign(signal)` |
| `external_trade_flow` | `external_trade_imbalance_consensus` | `+sign(signal)` |
| `fair_accel_100ms` | `fair_accel_100ms_bps` | `+sign(signal)` |
| `fair_gap_reversion` | `bitget_gap_to_fair_bps` | `−sign(gap)` (reversion) |
| `leader_gap_{100,200,500,1000}ms` | `leader_gap_{X}ms_bps` | `+sign(signal)` |

Quality gate (recoverable from handoff + collector wiring):
`adjusted_fair_venue_count ≥ 2` AND `bitget_book_age_recv_ms ≤ 1000` AND `bitget_mid` finite & positive.

---

## 3. Exact methodology recovered

**Fully recovered from artifacts:**

- Target = Bitget mid forward log-return in bps, at horizons **100, 200, 500, 1000, 2000, 5000, 10000, 30000 ms** (verified: the `horizon_ms` column in every block-level CSV contains exactly this set).
- Evaluation unit = (session_id, asset). **22 blocks** total (14 old24 × 8 new12).
- Quantiles evaluated = **0.8, 0.9, 0.95** (verified in the `q` column of every block-level CSV; q90 is the primary decision quantile per report).
- Feature set (simple) = **12 features** listed in §2 table.
- Composite feature set = **10 composites**: `depthBoth, depthL1_extOFI, gap_depthBoth, gap_depthL1, gap_depth_extOFI, gap_extOFI, gap_leader1000, gap_localOFI, gap_localTrade, leader1000_extOFI`.
- Per-block outputs stored: `threshold, N, mean_signed_bps, hit_rate` (+ `median_signed_bps, mean_abs_move` for simple).
- Economic hurdle **15 bps** is a governance boundary; recovered from the report and consistent with the handoff.
- Horizon-domain split: **100 ms – 5 s = execution-timing**, **10 s – 30 s = markout / adverse-selection**.
- Positive-block criterion is applied to `mean_signed_bps`: block counts as positive iff `mean_signed_bps > 0` (verified against the block-level values behind every `positive_blocks` count in the aggregates).

**Fully recovered golden references (regression targets, DO NOT hard-code as outputs):**

- Complete per-block (session,asset,feature,horizon,q) numeric results for all 22 blocks in `simple_q80_q90_q95_block_level_24h.csv` + `simple_block_level_new12.csv` (24H portion + NEW12 portion).
- Complete per-block composite results in `composite_q80_q90_q95_block_level_24h.csv` + `composite_block_level_new12.csv`.
- Aggregated q90/30s numbers: `fair_gap_reversion +0.660 bps (22/22)`, `depth_imbalance_l1 +0.616 bps (22/22)`, `depth_imbalance_l5 +0.636 bps (22/22)`, `external_ofi +0.342 bps (22/22)`, `leader_gap_1000ms +0.369 bps (22/22)`, `leader_gap_500ms +0.216 bps (18/22)`, `gap_localTrade +0.949 bps (22/22)`, `gap_extOFI +0.781 bps (22/22)`, `gap_depth_extOFI +0.807 bps (22/22)`, `gap_depthBoth +0.751 bps (22/22)`, `depthBoth +0.700 bps (22/22)`.

---

## 4. Composite definitions recovered

**Recovered:** the *NAMES* of the 10 composites and, from cross-checking the `threshold` column, the fact that every `gap_*` composite (`gap_depthBoth, gap_depthL1, gap_depth_extOFI, gap_extOFI, gap_leader1000, gap_localOFI, gap_localTrade`) uses the **same threshold value as `fair_gap_reversion`** (e.g. `0.44852834352759896` in block `20260905T073818Z_e44d99bd` BTC q=0.9 h=1000). This strongly indicates that the `gap_*` composites **gate event selection on the fair-gap quantile** and then evaluate a joint condition with the second component.

Similarly, `leader1000_extOFI` uses the same threshold as `leader_gap_1000ms` in each block, indicating gating on `leader_gap_1000ms`.

**NOT recovered:** the exact combination arithmetic. It is impossible to distinguish, from artifacts alone, whether e.g. `gap_localTrade` is:

- a) events where `|gap| ≥ q90(|gap|) AND sign(localTrade) agrees with −sign(gap)`;
- b) a weighted composite score `w1 · z(gap) + w2 · z(localTrade)` thresholded at q90;
- c) events where both signal quantiles cross their respective q90 with sign agreement;
- d) some other joint formulation.

The published aggregate `+0.949 bps` at q90/30s is fully consistent with several of these formulations. Without the original scoring script this is a **PROSE-DESCRIBED / CODE-UNSPECIFIED** definition.

`depthBoth` and `depthL1_extOFI` have their own composite threshold values (e.g. `depthBoth` threshold `0.8762714481640621`, distinct from either component and not a simple mean), which strongly suggests these composites are computed as a **new derived signal (e.g. `(l1 + l5)/2` or `combine(l1, extOFI)`) whose own quantile is then evaluated**. The derived signal formula is again NOT explicit anywhere in the artifacts.

**Composite weights:** no configuration file was shipped. Weights, normalization scheme (z-score? rank? none?) and sign convention are all IMPLICIT in the missing scoring script.

---

## 5. Missing definitions / artifacts (precise list)

Marked MISSING per project directive — none of these are to be filled by AI judgment:

1. **Historical validator script** (Python/notebook) that produced all `*_block_level_*.csv` outputs. No `.py`, `.ipynb`, `.yaml`, `.yml`, `.toml`, `.json` file is present in either checkpoint ZIP.
2. **Exact event-selection algorithm** for each simple feature: is the quantile computed on the raw signal `s`, on `|s|`, on the signed signal after applying the direction rule, or on the absolute-tail (`|s| ≥ q_q(|s|)`)? Tie-handling is not specified.
3. **NaN and missing-fresh handling** during event selection and forward return construction. The quality filter is documented; the precise ordering (filter before or after quantile fit?) is not.
4. **Forward-return alignment** at 30 000 ms is 300 grid rows: the exact policy for truncating the last-30 s of each session/asset is not specified anywhere in the artifacts. Row counts `N` per block imply *some* truncation (e.g. block `20260905T073818Z_e44d99bd` BTC has 431 998 grid rows across the 6 h session — at h=30 000 the event count drops to 340 for `depth_imbalance_l1` at q=0.9, consistent with tail-quantile filtering AND overlap enforcement AND end-of-session truncation, but the exact truncation rule is not deterministically recoverable).
5. **Overlap enforcement** (`≥ max(1 s, horizon)` spacing). The narrative describes this, but the tie-breaking algorithm (greedy earliest-first? largest-magnitude-first? random?) is not specified. Different orderings yield different `N`.
6. **Composite formulas** — the exact combination arithmetic for each of the 10 composites (see §4).
7. **Composite component normalization / sign convention** (z-score? rank-quantile? raw?).
8. **`localTrade` and `extOFI` alignment filter definition** when used inside `gap_localTrade`, `gap_extOFI`, `gap_localOFI`. The collector exposes `bitget_fair_trade_alignment` and `bitget_fair_ofi_alignment` (both = `sign(−gap) × sign(local_signal)`), but whether the composites use these binary flags, the raw signal magnitudes, or a threshold on the signal magnitude is NOT stated.
9. **Block aggregation weights** from block-level to feature-level. The report says "mean". Is it a plain mean of per-block `mean_signed_bps` (unweighted)? An N-weighted mean? A `N`-pooled recomputation? For BTC/ETH aggregation specifically, no policy is written.
10. **Dispersion-regime boundaries.** `disp_lo` and `disp_hi` columns are per-block. Are they computed per-session tertiles of `external_perp_dispersion_bps` on the *quality-filtered* grid? Or over *event rows only*? The precise definition is not exposed.
11. **Positive-block sign convention** at q95 tail where some entries are slightly negative (e.g. `leader_gap_500ms` at q95: 21/22). Whether ties (exactly 0) count as positive is undocumented.
12. **Final promotion / downgrade decision logic** — the report enumerates decisions per feature but the *rule* mapping (positive_share, mean_bps, min_block, min hit_rate, horizon-domain stability) to labels like `CONFIRMED_EXECUTION_FEATURE_CANDIDATE` vs `CONFIRMED_SECONDARY_EXECUTION_CANDIDATE` is qualitative, not numeric.
13. **OLD24 + NEW12 raw parquet grid data** — the 11 historical raw sessions are not attached and not present in SuperBot's runtime storage. Only the 12 NEW36 sessions are currently retained. Without the 11 historical grids, we cannot re-run the exact 24H / 36H analysis end-to-end from source to verify byte-for-byte reproduction.

---

## 6. Historical 36H reproduction — is it technically possible?

**No, not byte-for-byte and not from a clean slate.**

Two independent blockers:

1. **The exact scoring script is not present.** Any implementation now would be a *reconstruction from prose and structure*, not a recovery. Even if the reconstruction converges to the published aggregate numbers, it would not constitute EXACT_VALIDATOR_RECOVERABLE — small policy differences in overlap ordering, tie-handling, NaN treatment, or block aggregation weighting can move fourth-decimal numbers (the CP36 report itself flags this: *"Tiny threshold-value differences at the fourth decimal can occur from the exact base-row filtering order"*).
2. **The raw grids for 11 of the 22 historical blocks are not on hand.** The current SuperBot runtime holds only the 12 NEW36 3-hour sessions. The 7 OLD24 sessions (period WE/WD, dated 2026-09-05 to 2026-09-07) and the 4 NEW12 sessions (2026-09-07 to 2026-09-09) referenced in `session_audit_36h.csv` are NOT re-uploadable from the supplied artifacts — the ZIPs contain only summarized CSVs, no raw parquet grids.

**Partial reproduction possibility (not yet authorized):** given the block-level CSVs alone, one COULD verify aggregate-level arithmetic downstream (e.g. `all36_mean_bps = unweighted or N-weighted mean of the 22 per-block mean_signed_bps`?). This is a hypothesis test, not an implementation. It is out of scope for a READ-ONLY audit.

---

## 7. Is the OLD36 raw data required?

**Yes**, if the objective is deterministic reproduction of the historical checkpoint outputs. Without the 11 historical raw parquet grids, an EXACT_VALIDATOR run is impossible on the OLD24 + NEW12 blocks that produced the frozen 36H numbers.

For NEW36 forward analysis alone, the currently retained 12 real 3H sessions are sufficient for a forward run of any recovered/reconstructed validator — but that run cannot regression-check itself against the frozen 24H/36H numbers.

---

## 8. Do checkpoint artifacts contain sufficient per-block data?

**Partially.** The checkpoint artifacts contain the *outputs* per (session, asset, feature, horizon, q):

- `threshold` — the quantile value on whatever signal series was used
- `N` — post-overlap post-filter event count
- `mean_signed_bps` — the primary regression target
- `median_signed_bps` — simple features only
- `hit_rate` — direction agreement
- `mean_abs_move` — simple features only

They **do NOT contain**:

- The intermediate event-row-level selections (which grid rows were selected as events at each quantile).
- The per-event forward returns.
- The pre-overlap event pools.
- The per-block dispersion-tertile boundaries as scalars per session (only the per-block window `disp_lo, disp_hi` alongside each state row is exposed).
- The composite-formation intermediates (component z-scores or scores).

This is enough for **golden-reference regression checking** at aggregate and block levels, but not enough to reconstruct the exact algorithm.

---

## 9. Current NEW36 availability inside SuperBot

Verified via `/api/checkpoints` at audit time — all 12 registered NEW36 sessions are present, PASS-verdict, milestone-valid:

| # | session_id | present | latest_verdict | milestone_valid |
|---|---|---|---|---|
| 1 | 20260910T123759Z_5ab0c1b2 | True | PASS | True |
| 2 | 20260911T040213Z_43a798b7 | True | PASS | True |
| 3 | 20260911T081446Z_28b5a901 | True | PASS | True |
| 4 | 20260911T113249Z_276d8c3b | True | PASS | True |
| 5 | 20260911T145009Z_37923a7a | True | PASS | True |
| 6 | 20260911T220737Z_5d45563e | True | PASS | True |
| 7 | 20260912T072730Z_adb96088 | True | PASS | True |
| 8 | 20260912T163326Z_16b9ca74 | True | PASS | True |
| 9 | 20260912T213151Z_9f9a0088 | True | PASS | True |
| 10 | 20260914T050815Z_3b68aabf | True | PASS | True |
| 11 | 20260914T133522Z_6141ec8b | True | PASS | True |
| 12 | 20260914T192945Z_1b899057 | True | PASS | True |

Milestones: `OLD36=36.0/36  NEW12=12.0/12  TOTAL48=48.0/48  NEW36=36.0/36  TOTAL72=72.0/72  data_qa_ready=True`.

Retained raw ZIPs are deduplicated per content-addressed storage (`raw_storage.py`); every NEW36 raw session is available for downstream analysis.

---

## 10. Final status

# `PARTIALLY_RECOVERABLE`

**Summary:**

- The frozen collector source and every relevant grid field are fully recoverable and formally locked (`FROZEN_COLLECTOR_SHA256 = e924edc1…1265a3`).
- All frozen governance parameters (horizons, quantiles, quality filter, 15 bps hurdle, evaluation block definition, sign conventions per simple feature) are recoverable from a combination of the handoff and the artifacts.
- Complete block-level golden reference values for the 22 historical blocks are available for regression checking.

**However**, the following prevent a status of `EXACT_VALIDATOR_RECOVERABLE`:

1. No original analysis script / notebook / configuration file is present in either checkpoint ZIP.
2. The **exact composite formulas and weights** are not written down anywhere — the outputs recover the composite *names* and their *event-selection thresholds*, but not the combination arithmetic, component normalization or sign convention.
3. The **overlap-enforcement ordering, tie-handling, NaN-handling, end-of-session truncation, dispersion-tertile computation, and BTC/ETH block-aggregation weighting** are all only prose-described.
4. The **raw parquet grids for the 11 OLD24 + NEW12 historical blocks** are not attached and are not in SuperBot's storage. Byte-for-byte historical reproduction is therefore infeasible even if the algorithm were fully specified.

**To advance to `EXACT_VALIDATOR_RECOVERABLE` the user must supply at least ONE of the following (and preferably all four):**

- **A.** The original analysis script / notebook that produced the CP24 and CP36 CSVs (definitive).
- **B.** A written, deterministic composite-formula / weights / normalization specification (mid-priority; needed alongside A or in place of it).
- **C.** The 11 OLD24 + NEW12 raw parquet session grids (needed to run any recovered validator end-to-end against the frozen numbers).
- **D.** A written specification of overlap-enforcement ordering, tie-handling, NaN-handling and end-of-session truncation (small file; complements A/B).

Without at least A **or** (B + C + D), any implementation of `FrozenAnalysisEngine` would be a reconstruction rather than a recovery, and would violate the project directive *"Do not implement approximations."*

`FrozenAnalysisEngine` remains `NOT_CONFIGURED`. **STOP** pending user review of this audit.
