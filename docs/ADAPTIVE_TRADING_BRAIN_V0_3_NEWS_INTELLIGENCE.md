# SuperBot Adaptive Trading Brain v0.3 — News Intelligence

Base branch: `adaptive-brain-v0`
Expected base HEAD: `3ea1a6e5469bc97e07ff756bdbe3c4705a423e65`

This bundle includes the complete v0.2 incremental layer plus the first News
Intelligence Module.

## News Intelligence behavior

The module continuously ingests configured news/evidence when the runtime
network switch is enabled.

Verified official default feeds:
- U.S. SEC press releases RSS
- Federal Reserve all press releases RSS
- Federal Reserve monetary-policy RSS

Additional publishers such as CoinDesk and CryptoSlate can be added through
`SUPERBOT_BRAIN_RSS_FEEDS_JSON` when a stable public feed is available.
Reuters/Bloomberg should be connected only through a licensed/authorized feed
or API; the module must not bypass paywalls or scrape restricted content.

Each news item is:
1. content-deduplicated;
2. mapped to tracked assets when possible;
3. classified by event type;
4. scored for sentiment;
5. scored for expected volatility;
6. persisted as a news assessment;
7. aggregated into an asset-level policy.

Event classes include:
- SEC_REGULATION
- FED_RATES
- HACK_EXPLOIT / SYSTEMIC_HACK
- REGULATION
- EARNINGS
- ETF
- MACRO
- MARKET_STRUCTURE
- GENERAL_NEWS

## Automatic bounded policy

The NewsPolicy can:
- reduce paper risk size when volatility risk rises;
- apply a bounded long/short ensemble-weight multiplier when sentiment is strong;
- pause new entries during critical events;
- flag FORCE_EXIT for sufficiently negative critical events;
- automatically close affected open PAPER positions when a current market tick
  exists.

It cannot:
- change hard RiskGovernor limits;
- enable live trading;
- send exchange orders;
- add private exchange credentials;
- withdraw funds.

## Important limitation

The current sentiment/event classifier is deterministic/rule-based. It is the
safety-first first stage, not the final semantic intelligence layer. A later
model-adviser stage can add LLM/ML semantic classification and source
calibration while retaining the same bounded NewsPolicy contract.

## Tests

The complete v0 + v0.2 + News Intelligence tree passes 17 focused tests.
