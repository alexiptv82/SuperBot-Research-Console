# SuperBot Adaptive Trading Brain v0.4

Expected repository:
`alexiptv82/SuperBot-Research-Console`

Expected branch:
`adaptive-brain-v0`

Expected base HEAD:
`71b2443f524d2947e6a0b87e5075cfa7d780155d`

## Purpose

v0.4 turns the existing V0.2/V0.3 ingestion and paper substrate into a more
selective adaptive decision system. The design principle is:

**ingest broadly, trust selectively, adapt within hard limits.**

## New capabilities

### 1. Time-decayed source reputation

Every model/trader/expert can accumulate outcome history.

Reputation uses:
- realized directional correctness;
- bounded reward;
- Bayesian shrinkage toward neutral for small samples;
- exponential time decay;
- source error history;
- bounded influence multiplier.

New/small-sample sources therefore cannot immediately dominate the ensemble.

### 2. Drift detection

Recent source performance is compared with the source's older baseline.

A source can be flagged when recent:
- accuracy deteriorates materially;
- average reward deteriorates materially.

Detected drift reduces source influence before the ensemble.

### 3. Regime-aware strategy router

Before ensemble scoring, every expert signal can be adjusted using:
- time-decayed source reputation;
- drift status;
- optional regime affinity in signal metadata.

All router multipliers are bounded.

The router records its multipliers in the immutable decision snapshot for
auditability.

### 4. Feedback-loop protection

Source reputation and ensemble learning use the source's original confidence,
not confidence after router amplification.

This prevents source reputation from recursively rewarding its own multiplier.

### 5. Champion / challenger model registry

Models can be registered with:
- model ID;
- version;
- expert ID;
- CHAMPION / CHALLENGER / INACTIVE role;
- metadata.

A challenger can only qualify for promotion after:
- minimum sample size;
- quality advantage;
- reward advantage;
- no detected recent drift.

Promotion remains an explicit authenticated action in v0.4.
No source code is rewritten and no arbitrary model binary is downloaded.

### 6. Model-adviser contract

A vendor-neutral in-process adviser interface is added.

The runtime does not include paid provider credentials or a vendor-specific
LLM client in this tranche. Future OpenAI-compatible/other advisers can connect
behind this interface while preserving the same ExpertSignal contract.

### 7. Automatic PAPER lifecycle

Open PAPER positions are continuously marked on incoming market ticks.

The lifecycle can automatically perform PAPER-only:
- STOP_LOSS_PAPER;
- TAKE_PROFIT_PAPER;
- TIME_EXIT_PAPER.

Mark-to-market snapshots are persisted with throttling to avoid writing the
database on every high-frequency market tick.

There is still no live execution client.

## New API surface

Authenticated endpoints include:
- `GET /api/brain/reputation`
- `GET /api/brain/reputation/{source_id}`
- `GET /api/brain/drift`
- `GET /api/brain/drift/{source_id}`
- `GET /api/brain/models`
- `POST /api/brain/models/register`
- `POST /api/brain/models/promotion`
- `GET /api/brain/advisers`
- `GET /api/brain/paper/marks/{position_id}`

## Safety

v0.4 does not add:
- exchange private API credentials;
- order placement;
- order cancellation;
- withdrawal;
- live execution;
- learner-controlled hard risk limits;
- self-modifying code.

`RiskGovernor` remains superior to the adaptive modules.

The frozen Research Console remains read-only and separate.

## Version

`AdaptiveBrain.VERSION = 0.4.0`

## Focused validation

Complete local focused suite:
`24 passed`

This is not a profitability claim and is not authorization for live trading.
