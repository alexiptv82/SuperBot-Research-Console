# SuperBot Adaptive Brain integration bundle

Built against authoritative repository HEAD:
ec60720bc78ba1885501847dc27d097926316428

New code lives under `backend/adaptive_brain/`; existing frozen recovery files are untouched.

After adding the package, apply `patches/server_integration.patch` to expose:
- GET /api/brain/status
- POST /api/brain/observe
- POST /api/brain/decide
- POST /api/brain/outcome
- GET /api/brain/weights
- GET /api/brain/metrics

Default mode is PAPER.
`SUPERBOT_BRAIN_MODE=LIVE` fails closed by design in v0.
