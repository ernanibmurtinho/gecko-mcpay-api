# Sprint 25 — Schema Fix + Test Hardening + Wallet UX

**Status:** shipped
**Date:** 2026-05-04

## Problem

1. `ValidationReport` and all nested `ResearchResult` types were absent from the production OpenAPI schema — FastAPI generated a bare `dict` because `GET /sessions/{session_id}/result` had `-> dict[str, Any]` instead of `response_model=ResearchResult`. Consequence: `gap_explanation` never appeared in the schema regardless of how many times the API was deployed.

2. `test_pricing.py` and `test_middleware.py` were broken because their fixtures called `os.environ.pop("RESEARCH_BASIC_PRICE")` to exercise code defaults, but `load_dotenv()` (called at module re-import) reloaded the local `.env` value (`$0.10`) right back in. Tests expected `$20.00`.

3. ECS deploys appeared to succeed but the running container was unchanged. Root cause: `CF_IMAGE="${ECR_URI}:${ENVIRONMENT}-latest"` never changes between deploys, so CloudFormation sees no diff and skips the task definition update.

4. `gecko-mcp quickstart` hard-failed when `~/.agentwallet/config.json` was absent. Self-custody path (`~/.gecko/wallet.json`) existed in code but was unreachable from quickstart.

## What Shipped

### S25-SCHEMA-01 — OpenAPI schema fix
`GET /sessions/{session_id}/result` now declares `response_model=ResearchResult`. Schema grows from 19 to 35 components. `ValidationReport` exposes all 8 fields including `gap_explanation`. `ResearchResult` and all nested types are now in the API contract.

File: `packages/gecko-api/src/gecko_api/main.py`

### S25-TEST-01 — Pricing/middleware test hardening
Both `test_pricing.py` and `test_middleware.py` fixtures now pin `X402_MODE`, `X402_NETWORK`, `RESEARCH_BASIC_PRICE`, and `RESEARCH_PRO_PRICE` explicitly before reimporting the app. `load_dotenv()` can no longer overwrite these with local `.env` values. Previously broken 22 tests now all pass.

Files: `tests/api/test_pricing.py`, `tests/api/test_middleware.py`

### S25-DEPLOY-01 — ECS image tag forces CloudFormation update
`IMAGE_TAG` now includes a Unix timestamp (`${ENVIRONMENT}-${SHA}-${date +%s}`). `CF_IMAGE` uses the versioned tag instead of the static `production-latest`. Every `./infra/deploy.sh` run creates a new image tag, which CloudFormation treats as a parameter change and updates the ECS task definition + triggers a rolling deployment.

File: `infra/deploy.sh`

### S25-WALLET-01 — Quickstart self-custody fork
`_check_wallet()` detects `GECKO_WALLET_PROVIDER` (env or `~/.gecko/config.toml`) and routes to either `_check_wallet_frames()` (frames.ag) or `_check_wallet_self()` (local keypair). Self-custody path shows public key, USDC balance, and Phantom deep link if balance is 0.

File: `packages/gecko-mcp/src/gecko_mcp/quickstart.py`

### S25-WALLET-02 — `wallet new` outputs Phantom deep link + terminal QR
`fund_url(pubkey)` returns a Phantom-compatible `solana:` URI pre-filled with the wallet's pubkey and $5 USDC. `qr_code_ascii(url)` wraps it in a terminal-printable QR code (via `qrcode` with `TerminalImage`). `gecko-mcp wallet new` now prints both the deep link and the QR so users on a phone can fund without copy-paste.

Files: `packages/gecko-mcp/src/gecko_mcp/wallet_self_custody.py`, `packages/gecko-mcp/pyproject.toml`

### S25-WALLET-03 — `wallet switch --provider` + doctor wallet section
`gecko-mcp wallet switch --provider [frames|self]` writes `GECKO_WALLET_PROVIDER` to `~/.gecko/config.toml` so the preference survives shell restarts. `check_wallet()` in `doctor.py` now shows `payments:provider`, `payments:address`, and `payments:balance` (live USDC fetch for self-custody mode).

Files: `packages/gecko-mcp/src/gecko_mcp/wallet.py`, `packages/gecko-mcp/src/gecko_mcp/doctor.py`

## Acceptance Criteria (verified)

- [x] `curl https://api.geckovision.tech/openapi.json` shows `ValidationReport` with 8 properties including `gap_explanation` (after deploy)
- [x] `uv run pytest` → 1674 passed, 0 failures
- [x] `gecko-mcp wallet switch --provider self` writes config, quickstart routes to self-custody path
- [x] `gecko-mcp wallet new` outputs Phantom deep link and terminal QR
- [x] `gecko-mcp doctor` shows `payments:provider`, `payments:address`, `payments:balance`
- [x] `./infra/deploy.sh` always produces a new image tag and forces a CloudFormation update

## Twit.sh /users/tweets 404 (task #5)
Already resolved as of Sprint 14. `DEFAULT_CATALOG["userTweets"]["path"]` = `/tweets/user` (not `/users/tweets`). The old path only appears in code comments. No action needed.
