# MULTIVENUE MICROSTRUCTURE V2 — CHECKPOINT 36H

## Verdict

**Execution/adverse-selection structure: CONFIRM / ADVANCE TO 72H VALIDATION.**

**Standalone directional alpha: REJECT (unchanged).** All observed effects remain far below the official 15 bps directional cost hurdle.

**Live trading: NOT AUTHORIZED.**

## Dataset

- 36h total quantitative coverage.
- 11 sessions total, BTC + ETH, synchronized 100 ms grid.
- Frozen collector SHA256: `e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3`.
- New 12h batch: four 3h sessions across 8–9 September 2026, with night, morning, afternoon and evening coverage.
- All four new sessions: exit code 0, watchdog false, 360/360 grid/book/trade chunks, zero sequence holes, writer errors 0.
- New batch missed ticks: 134 / 432,000 = 0.0310%.
- All-36h missed ticks: 350 / 1,296,000 = 0.0270%.
- All-36h grid rows: 2,591,300 / 2,592,000.
- All-36h books received: 90,847,594; books persisted: 22,124,814; trades: 11,151,364; websocket messages: 97,733,849.
- All-36h websocket errors/reconnects: 6/6; all recovered, with no file-hole or writer-loss evidence.

## Predeclared q90 / 30s comparison

| feature            |   old24_mean_bps | old24_positive_blocks   |   new12_mean_bps | new12_positive_blocks   |   all36_mean_bps | all36_positive_blocks   |   all36_N |
|:-------------------|-----------------:|:------------------------|-----------------:|:------------------------|-----------------:|:------------------------|----------:|
| fair_gap_reversion |            0.65  | 14/14                   |            0.676 | 8/8                     |            0.66  | 22/22                   |      5146 |
| depth_imbalance_l1 |            0.648 | 14/14                   |            0.561 | 8/8                     |            0.616 | 22/22                   |      6342 |
| depth_imbalance_l5 |            0.582 | 14/14                   |            0.728 | 8/8                     |            0.636 | 22/22                   |      6315 |
| external_ofi       |            0.291 | 14/14                   |            0.444 | 8/8                     |            0.342 | 22/22                   |      8032 |
| leader_gap_500ms   |            0.274 | 14/14                   |            0.108 | 4/8                     |            0.216 | 18/22                   |      7050 |
| leader_gap_1000ms  |            0.337 | 14/14                   |            0.426 | 8/8                     |            0.369 | 22/22                   |      6534 |

### Interpretation

The core remains strong across the new multi-day weekday sample:
- `fair_gap_reversion`: +0.676 bps in the new 12h, 8/8 blocks positive; 22/22 all-36h.
- `depth_imbalance_l1`: +0.561 bps new, 8/8; 22/22 all-36h.
- `depth_imbalance_l5`: +0.728 bps new, 8/8; 22/22 all-36h.
- `external_ofi`: +0.444 bps new, 8/8; 22/22 all-36h.
- `leader_gap_1000ms`: +0.426 bps new, 8/8; 22/22 all-36h.
- `leader_gap_500ms` is the exception at the 30s markout horizon: +0.108 bps new but only 4/8 positive; 18/22 all-36h. It remains robust at shorter horizons and should be reclassified as short-horizon execution evidence rather than a 30s markout feature.

## Selected composites q90 / 30s

| feature          |   old24_mean_bps | old24_positive_blocks   |   new12_mean_bps | new12_positive_blocks   |   all36_mean_bps | all36_positive_blocks   |   all36_N |
|:-----------------|-----------------:|:------------------------|-----------------:|:------------------------|-----------------:|:------------------------|----------:|
| gap_localTrade   |            0.928 | 14/14                   |            0.98  | 8/8                     |            0.949 | 22/22                   |      3689 |
| gap_extOFI       |            0.746 | 14/14                   |            0.835 | 8/8                     |            0.781 | 22/22                   |      4712 |
| gap_depth_extOFI |            0.754 | 14/14                   |            0.888 | 8/8                     |            0.807 | 22/22                   |      4461 |
| gap_depthBoth    |            0.744 | 14/14                   |            0.761 | 8/8                     |            0.751 | 22/22                   |      4617 |
| depthBoth        |            0.643 | 14/14                   |            0.796 | 8/8                     |            0.7   | 22/22                   |      6221 |

Every selected composite remains positive in **8/8 new blocks** and **22/22 total blocks**.

## Threshold sensitivity at 30s — all 36h

- Fair gap: q80 +0.353 bps (21/22), q90 +0.660 (22/22), q95 +0.774 (22/22).
- L1 depth: q80 +0.567 (22/22), q90 +0.616 (22/22), q95 +0.626 (21/22). One q95 block is slightly negative; aggregate monotonicity remains.
- L5 depth: q80 +0.523, q90 +0.636, q95 +0.671; all 22/22.
- External OFI: q80 +0.204 (21/22), q90 +0.342 (22/22), q95 +0.463 (22/22).
- Leader 1000ms: q80 +0.242 (19/22), q90 +0.369 (22/22), q95 +0.439 (22/22).
- Leader 500ms: q80 +0.196 (22/22), q90 +0.216 (18/22), q95 +0.383 (21/22). This is not a clean 30s stability profile.

Strongest composite:
- `gap_localTrade`: q80 +0.696, q90 +0.949, q95 +1.189 bps; **22/22 positive at every tested quantile**.
- `gap_extOFI`: q90 +0.781; q95 +1.047; 22/22.
- `gap_depth_extOFI`: q90 +0.807; q95 +0.936; 22/22.

These are execution-scale effects, not standalone directional-alpha returns.

## Operational horizon profile

At q90, all selected core features are positive in **22/22 blocks from 100ms through 5s**, including `leader_gap_500ms`.

All-36h q90 mean signed bps at 1s / 5s:
- fair gap: +0.242 / +0.456
- L1 depth: +0.335 / +0.583
- L5 depth: +0.307 / +0.527
- external OFI: +0.206 / +0.295
- leader gap 500ms: +0.155 / +0.205
- leader gap 1000ms: +0.177 / +0.265

Selected composites are also 22/22 positive at every tested operational horizon from 1s through 5s:
- `gap_localTrade`: +0.577 @1s, +0.773 @5s
- `gap_extOFI`: +0.319 @1s, +0.621 @5s
- `gap_depth_extOFI`: +0.342 @1s, +0.657 @5s
- `gap_depthBoth`: +0.309 @1s, +0.587 @5s
- `depthBoth`: +0.328 @1s, +0.553 @5s

This strengthens the separation:
- **100ms–5s = primary execution-timing horizon**
- **10s–30s = adverse-selection / markout horizon**
- `leader_gap_500ms` should not be promoted as a reliable 30s markout feature.

## External dispersion

The new 12h again show stronger fair-gap response in the high-dispersion tertile, but the previous 12h weekday batch showed a different ordering. Therefore the earlier governance decision is unchanged:

`EXTERNAL_PERP_DISPERSION` → **RETAIN_REGIME_DIAGNOSTIC**, not a monotonic confidence/size multiplier.

All-36h q90 fair-gap means:
- 5s: high +0.591, mid +0.361, low +0.359 bps
- 10s: high +0.675, mid +0.451, low +0.446
- 30s: high +0.712, mid +0.483, low +0.497

The aggregate ordering is suggestive but not sufficient to override the cross-batch non-monotonicity.

## Registry update

- `MULTIVENUE_DIRECTIONAL_ALPHA` → **REJECT**
- `FAIR_GAP_REVERSION` → **CONFIRMED_EXECUTION_FEATURE_CANDIDATE**
- `BITGET_DEPTH_IMBALANCE_L1` → **CONFIRMED_EXECUTION_FEATURE_CANDIDATE**
- `BITGET_DEPTH_IMBALANCE_L5` → **CONFIRMED_EXECUTION_FEATURE_CANDIDATE**
- `EXTERNAL_OFI` → **CONFIRMED_SECONDARY_EXECUTION_CANDIDATE**
- `LEADER_GAP_1000MS` → **CONFIRMED_SECONDARY_EXECUTION_CANDIDATE**
- `LEADER_GAP_500MS` → **RETAIN_SHORT_HORIZON_EXECUTION_FEATURE; 30S CAVEAT**
- `LOCAL_TRADE_ALIGNMENT` → **CONFIRMED_CONFIRMATION_FILTER**
- `EXTERNAL_OFI_ALIGNMENT` → **CONFIRMED_CONFIRMATION_FILTER**
- `EXTERNAL_PERP_DISPERSION` → **RETAIN_REGIME_DIAGNOSTIC**
- `LIVE_TRADING` → **NOT AUTHORIZED**

## Decision

The 36h checkpoint passes the cross-day robustness test. Proceed toward **72h total validation** without changing the collector, signal definitions, quantile thresholds, composite logic or official 15 bps directional hurdle.

Before 72h, the execution/replay simulator architecture may be designed, but no feature weights or thresholds should be tuned on the current data.

## Method note

The old 24h portion is taken from the frozen 24h checkpoint outputs. The new 12h Parquet grids were screened with the same signal-direction, per-session×asset quantile, forward-mid and non-overlap/cooldown logic, cross-checked against an existing 24h session where reproduced N and mean-bps values were effectively identical for the selected primary/composite signals. Tiny threshold-value differences at the fourth decimal can occur from the exact base-row filtering order and do not affect the checkpoint verdict.
