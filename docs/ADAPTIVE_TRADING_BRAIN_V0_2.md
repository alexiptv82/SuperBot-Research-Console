# SuperBot Adaptive Trading Brain v0.2

Base repository branch:
`adaptive-brain-v0`

Expected base HEAD before integration:
`3ea1a6e5469bc97e07ff756bdbe3c4705a423e65`

## Scope

v0.2 adds the first continuously connected evidence/market layer and a realistic
paper-execution substrate without adding private exchange credentials or live
order execution.

## Added capabilities

- Bitget v3 **public** ticker WebSocket adapter.
- Always-on runtime service with reconnect/backoff and 30-second ping heartbeat.
- Latest market-state cache for configured symbols.
- Persistent market observations sampled into the brain memory.
- Online deterministic regime detector: INSUFFICIENT / RANGE / TREND_UP /
  TREND_DOWN / HIGH_VOL.
- Generic RSS/Atom read-only news ingestion for explicitly configured feeds.
- Persistent deduplicated evidence store for news/research/model/trader evidence.
- Source registry with signal count, correctness count, error count and last-seen.
- Paper portfolio simulator with bid/ask spread, configurable fee and slippage
  assumptions, risk-budget-based sizing and persistent fills/positions.
- Auth protection on the Adaptive Brain API by reusing the existing Research
  Console session verifier.
- New API endpoints for evidence, sources, market state, regime and paper
  portfolio operations.
- Learning fix: outcomes update experts from the immutable decision-time
  snapshot even when the original signal TTL has expired.

## Network safety

The Bitget adapter is public/read-only and accepts no API key.

Network ingestion is disabled by default for test/development reproducibility.
Enable only in the runtime environment with:

`SUPERBOT_BRAIN_NETWORK_ENABLED=true`

Optional runtime variables:

- `SUPERBOT_BRAIN_MARKET_SYMBOLS=BTCUSDT,ETHUSDT`
- `SUPERBOT_BRAIN_MARKET_INST_TYPE=usdt-futures`
- `SUPERBOT_BRAIN_MARKET_PERSIST_SECONDS=5`
- `SUPERBOT_BRAIN_RSS_POLL_SECONDS=300`
- `SUPERBOT_BRAIN_RSS_FEEDS_JSON=[...]`

No private Bitget channel, order endpoint, cancel endpoint or withdrawal path
is introduced.

## Current limits

- The regime detector is context, not a standalone alpha model.
- RSS evidence is stored/deduplicated; semantic extraction/model scoring is a
  later tranche.
- Paper execution is not yet automatically triggered by every decision.
- Paper fee/slippage defaults are simulation assumptions, not a claim about the
  user's actual account fee tier.
- No live trading is authorized in v0.2.

## Next tranche

- source calibration and decay;
- model-adviser adapters;
- feature promotion from validated Research Console findings;
- automatic paper lifecycle and mark-to-market;
- strategy/router layer conditioned on market regime;
- drift detection and champion/challenger model promotion.
