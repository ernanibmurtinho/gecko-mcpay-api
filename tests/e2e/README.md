# tests/e2e/

End-to-end smoke tests that hit a live `gecko-api` HTTP surface.

These run the **real** request pipeline (FastAPI app + x402 middleware +
trade-panel orchestration), so they need a deployed (or locally
running) server. They are **skipped by default** so a plain
`uv run pytest` never reaches over the network.

## Trading-oracle smoke (`test_trade_oracle_smoke.py`)

Drives the Phase 10A x402 dance against `/trade_research` and
`/trade_research/pro`:

1. POST without payment header  → assert 402 with v2 challenge in the
   `payment-required` *header* (not body).
2. Decode the challenge, build a stub-mode `PAYMENT-SIGNATURE` header,
   retry.
3. Assert HTTP 200 with `verdict in {act,pass,defer}`, valid
   `confidence`, ≥1 `turns` entry, plus `key_drivers` /
   `blocker_questions` / `dissent_count` on the envelope.

Also asserts both routes show up in `/.well-known/x402`.

### Run

Against the deployed prod surface:

```bash
GECKO_E2E_BASE_URL=https://api.geckovision.tech \
  uv run pytest tests/e2e/test_trade_oracle_smoke.py -v -m e2e_smoke
```

Against a local stack (`uvicorn gecko_api.main:app --reload` from the
repo root in another shell):

```bash
GECKO_E2E_BASE_URL=http://localhost:8000 \
  uv run pytest tests/e2e/test_trade_oracle_smoke.py -v -m e2e_smoke
```

### Constraints

- Server must be in `X402_MODE=stub`. The test submits a stub payment
  payload; a live-mode flip would require a real signer (out of scope
  here, and the founder has not approved a live flip — see memory note
  `project_x402_stub_then_live`).
- The pro tier currently produces the same `TradePanelVerdict`-shaped
  envelope as basic; if a future schema split adds a top-level
  `backtest` field, update `_assert_verdict_shape` in the test.

## Tavily falsifier (paired script)

The companion adversarial probe lives at
[`scripts/falsifier_tavily.py`](../../scripts/falsifier_tavily.py).
That script asks: *does Gecko's verdict actually differ from a one-shot
Tavily search on the same question?* See the script docstring for run
instructions and the result interpretation.
