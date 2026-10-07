# SuperBot Adaptive Trading Brain v0

This layer is the first executable foundation for the adaptive "brain" requested for SuperBot.
It stays separate from the frozen Research Console and treats the Console as a read-only research substrate.

Current v0 capabilities:
- persistent brain memory in a separate SQLite database;
- ingestion schema for market, news, model, trader, research and on-chain observations;
- multi-expert ensemble for models, traders and strategies;
- bounded online expert-weight learning from realized outcomes;
- abstention when evidence is weak or conflicting;
- hard risk governor above the learner;
- read-only bridge to existing Stage3 research artifacts;
- framework for always-on approved network feeds;
- API surface for observe / decide / outcome / weights / metrics.

Safety boundary:
- PAPER/RESEARCH only;
- no order placement endpoint;
- no exchange credentials;
- no learner-controlled risk limits;
- no self-modifying code;
- no direct conversion of historical diagnostics into trade instructions.

Optimization objective:
risk-adjusted expectancy under hard drawdown/exposure constraints. Hit rate is monitored but is not the sole objective.

Next tranche:
1. public real-time market-data adapters;
2. news/event ingestion with provenance, freshness and deduplication;
3. curated trader/partner feeds with survivorship-bias tracking;
4. model-adviser adapters;
5. live feature service built from validated Research Console discoveries;
6. paper portfolio simulator with fees, spread, slippage and latency;
7. regime detector and strategy router;
8. calibration, drift and source-health monitoring;
9. only after paper validation, a separately reviewed execution adapter.
