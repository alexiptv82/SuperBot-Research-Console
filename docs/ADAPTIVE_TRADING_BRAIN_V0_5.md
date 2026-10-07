# SuperBot Adaptive Trading Brain v0.5

Repository:
`alexiptv82/SuperBot-Research-Console`

Expected branch:
`adaptive-brain-v0`

Expected base HEAD:
`f165707458fa1789d276255a322cd22bb5d344c2`

## Purpose

v0.5 connects the adaptive modules into a continuous PAPER-learning loop and
adds real partner/trader signal ingestion plus provider-neutral remote model
advisers.

The runtime principle remains:

**observe -> evaluate -> decide -> PAPER execute -> close -> attribute outcome -> learn**

No live exchange execution is introduced.

## 1. Continuous PAPER orchestrator

`ContinuousPaperOrchestrator` performs:

1. process current PAPER lifecycle;
2. attribute newly closed PAPER trades back to their decision;
3. update expert/source learning once;
4. build regime + news context;
5. collect model adviser signals;
6. collect fresh partner/trader signals;
7. require a minimum signal count;
8. call the Adaptive Brain;
9. optionally open a PAPER position;
10. enforce a per-symbol decision cooldown and one-open-position guard.

Outcome attribution is deduplicated by decision ID.

## 2. Safety defaults

Automatic PAPER opening is disabled by default:

`SUPERBOT_BRAIN_AUTO_PAPER=false`

Market/news networking also remains disabled by default unless separately
enabled by the existing runtime switch.

Therefore merely deploying v0.5 does not cause automatic PAPER trades or
external model calls.

## 3. Partner / trader signals

Authenticated API can accept normalized partner signals.

Signals carry:
- partner ID;
- asset;
- direction;
- confidence;
- expected edge;
- TTL;
- metadata.

The store keeps persistent history while only the latest fresh signal per
partner is used by the orchestrator.

This means trader/partner inputs enter the same reputation, drift and learning
framework as model signals after outcomes are realized.

## 4. Built-in advisers

Two deterministic baseline advisers are registered:

- `regime-momentum@1.0`
- `news-policy@1.0`

They exist to exercise and validate the live decision pipeline. They are not a
claim of trading edge.

## 5. Remote model adviser bridge

`RemoteJSONAdviser` is a provider-neutral HTTPS model bridge.

It supports real external model advisers if explicitly configured through:

`SUPERBOT_BRAIN_REMOTE_ADVISERS_JSON`

No remote adviser is configured by default.

Each remote adviser has:
- adviser ID;
- version;
- HTTPS URL;
- optional credential environment-variable name;
- timeout;
- minimum call interval;
- maximum calls per hour;
- enabled/disabled flag.

Non-local HTTP endpoints are rejected; remote endpoints must use HTTPS.

Credentials are never stored in repository source. If a bridge needs a bearer
token, the source only stores the environment-variable NAME, not the secret.

This allows future connection to free/local model bridges or paid providers
without coupling the Brain to a vendor.

## 6. Adviser fault isolation

Model advisers execute concurrently behind `AdviserHub`.

A timeout or exception from one adviser is isolated and does not fail the whole
decision cycle. Adviser errors are surfaced in runtime status.

## 7. Cost controls

Remote adviser calls can be bounded by:

- `min_interval_seconds`;
- `max_calls_per_hour`;
- adviser `enabled=false` by default.

This is important when a future adviser points to a paid model.

## 8. PAPER outcome learning

When STOP_LOSS_PAPER / TAKE_PROFIT_PAPER / TIME_EXIT_PAPER closes a position,
the orchestrator derives:

- raw market move in bps;
- realized PAPER PnL in bps;

and calls `brain.record_outcome()` once.

Critical-news PAPER force exits also feed the same learning path.

## 9. Risk state

Gross exposure is calculated from all open PAPER positions.
Symbol exposure is calculated only from the target symbol.

The hard RiskGovernor remains superior.

## 10. New authenticated endpoints

- `POST /api/brain/partner-signal`
- `GET /api/brain/partner-signals/{symbol}`
- `GET /api/brain/orchestrator`
- `POST /api/brain/orchestrator/cycle`

Existing `/api/brain/advisers` now includes adviser health/error data and
remote-adviser configuration status.

## 11. Version

`AdaptiveBrain.VERSION = 0.5.0`

## 12. Focused validation

Local focused suite:

`33 passed`

No live trading, private exchange API, withdrawal or real order endpoint is
introduced.
