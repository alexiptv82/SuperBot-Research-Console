# MULTIVENUE MICROSTRUCTURE V2 — CHECKPOINT 24H

## Verdict

**Execution/adverse-selection structure: CONFIRM.**

**Standalone directional alpha: REJECT (unchanged).** The observed effects remain far below the official 15 bps directional cost hurdle and must not be promoted as standalone trade signals.

**Live trading: NOT AUTHORIZED.**

## Dataset

- 24h target research coverage: 12h weekend + 12h weekday/session-diverse validation.
- 7 quantitative sessions, BTC and ETH, 100 ms synchronized grid.
- Same frozen collector for every session: `e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3`.
- All sessions: exit code 0, watchdog false, no file-sequence holes, writer errors 0.
- Total sampler missed ticks: 216 / 864,000 = 0.0250%.
- Total books received: 51,888,542.
- Total books persisted: 13,549,797.
- Total trades: 6,016,147.
- Total websocket messages: 55,660,090.
- Two brief recovered reconnects in weekday data (Binance Public W3; Bybit W4); neither caused chunk loss or session failure.

## Predeclared q90 / 30s validation

| Feature | Weekend 12h | Weekday 12h | All 24h | Positive blocks all 24h |
|---|---:|---:|---:|---:|
| fair_gap_reversion | +0.608 bps | +0.677 bps | +0.650 bps | 14/14 |
| depth_imbalance_l1 | +0.553 | +0.720 | +0.648 | 14/14 |
| depth_imbalance_l5 | +0.516 | +0.633 | +0.582 | 14/14 |
| external_ofi | +0.218 | +0.365 | +0.291 | 14/14 |
| leader_gap_500ms | +0.249 | +0.297 | +0.274 | 14/14 |
| leader_gap_1000ms | +0.234 | +0.426 | +0.337 | 14/14 |

The weekday validation did not collapse. Every primary/secondary selected feature is positive in all 8 new session×asset blocks at q90/30s.

## Selected composites q90 / 30s

| Composite | Weekend 12h | Weekday 12h | All 24h | Positive blocks all 24h |
|---|---:|---:|---:|---:|
| gap_localTrade | +0.773 bps | +1.020 bps | +0.928 bps | 14/14 |
| gap_extOFI | +0.730 | +0.756 | +0.746 | 14/14 |
| gap_depth_extOFI | +0.703 | +0.787 | +0.754 | 14/14 |
| gap_depthBoth | +0.668 | +0.792 | +0.744 | 14/14 |
| depthBoth | +0.562 | +0.704 | +0.643 | 14/14 |

## Threshold sensitivity at 30s

Weekday validation:
- fair gap: q80 +0.330 (7/8), q90 +0.677 (8/8), q95 +0.799 (8/8).
- L1 depth: q80 +0.565, q90 +0.720, q95 +0.756; all 8/8.
- L5 depth: q80 +0.540, q90 +0.633, q95 +0.623; all 8/8. q95 is slightly below q90, so strict monotonicity is imperfect but the effect remains stable.
- external OFI: q80 +0.112 (7/8), q90 +0.365 (8/8), q95 +0.545 (8/8).

All-24h:
- fair gap: q80 +0.341 (13/14), q90 +0.650 (14/14), q95 +0.750 (14/14).
- L1 depth: +0.509, +0.648, +0.699; all 14/14.
- L5 depth: +0.514, +0.582, +0.605; all 14/14.
- external OFI: +0.128 (13/14), +0.291 (14/14), +0.418 (14/14).

The strongest q95 composite is `gap_localTrade`: weekday +1.396 bps and all-24h +1.217 bps. This is meaningful at execution scale but still far below the 15 bps standalone directional hurdle.

## Horizon profile

At q90, weekday effects generally strengthen from 1s toward 30s. Fair-gap reversion, L1/L5 depth imbalance, external OFI, and leader-gap features remain positive across all 8 new blocks at the principal horizons. This supports a short-horizon execution/adverse-selection interpretation rather than a standalone alpha interpretation.

## Dispersion caveat

The weekend observation that **high external-perp dispersion was the strongest fair-gap regime did not replicate as an ordering rule**.

Weekend q90/30s fair-gap by dispersion regime:
- high: +0.723 bps (6/6)
- mid: +0.273 (5/6)
- low: +0.228 (5/6)

Weekday:
- high: +0.629 (8/8)
- mid: +0.538 (8/8)
- low: +0.723 (8/8)

Therefore external dispersion remains useful as a regime/context diagnostic, but **must not yet be promoted as a monotonic high-dispersion confidence multiplier**.

## Registry decision

- `MULTIVENUE_DIRECTIONAL_ALPHA` → **REJECT**.
- `FAIR_GAP_REVERSION` → **CONFIRMED_EXECUTION_FEATURE_CANDIDATE**.
- `BITGET_DEPTH_IMBALANCE_L1` → **CONFIRMED_EXECUTION_FEATURE_CANDIDATE**.
- `BITGET_DEPTH_IMBALANCE_L5` → **CONFIRMED_EXECUTION_FEATURE_CANDIDATE**, minor q95 monotonicity caveat.
- `EXTERNAL_OFI` → **CONFIRMED_SECONDARY_EXECUTION_CANDIDATE**.
- `LEADER_GAP_500MS/1000MS` → **CONFIRMED_SECONDARY_EXECUTION_CANDIDATE**.
- `LOCAL_TRADE_ALIGNMENT` → **CONFIRMED_CONFIRMATION_FILTER**.
- `EXTERNAL_OFI_ALIGNMENT` → **CONFIRMED_CONFIRMATION_FILTER**.
- `EXTERNAL_PERP_DISPERSION` → **RETAIN_REGIME_DIAGNOSTIC**.
- `LIVE_TRADING` → **NOT AUTHORIZED**.

## Main limitation

The 12h weekday validation comes from one calendar Monday across night, morning, afternoon, and evening. It materially improves regime diversity versus weekend-only data but is not yet multi-day weekday validation.

## Next checkpoint

Proceed to **36h total** by collecting another 12h as four 3h sessions on different weekdays, ideally spread across at least two calendar days. Keep the collector hash and methodology frozen. Do not tune thresholds or composites on the new data.

If the 36h result remains consistent, proceed toward 72h validation. Only after that should `EXECUTION_SIGNAL_V1` be tested in paper/replay execution against a neutral baseline using implementation shortfall, slippage vs arrival mid, markouts, maker fill rate, adverse selection, cancel/replace behavior, missed-fill opportunity cost, and net bps saved per execution.
