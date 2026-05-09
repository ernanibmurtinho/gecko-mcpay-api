# scripts/

Operator-run scripts. Not imported by the API or MCP server. Each is a
thin wrapper around `gecko-core` (or a foreign API) for a one-off
purpose — diagnostics, smokes, ablations, ingest runs.

## Highlights

| Script | Purpose |
|---|---|
| `e2e_smoke.py` | Sprint 7 dogfood loop against a running gecko-api: `/research → /scaffold → /plan → /pulse`, asserts `>= 3` stub_-prefixed receipts. CI uses this. |
| `falsifier_tavily.py` | **Task 8** — Gecko vs Tavily on 5 fixed Solana-DeFi questions. See section below. |
| `live_preflight.sh` | Pre-flip checklist for X402 live mode. |
| `trading_oracle/run.py` | One-shot $20 paid ingest from paysh + bazaar (Phase 2 of the trading-oracle plan). |
| `voyage_chunk_ab.py` | A/B retrieval quality probe, Voyage vs OpenAI embeddings. |

## Tavily falsifier (`falsifier_tavily.py`)

Adversarial probe paired with the e2e smoke at
[`tests/e2e/test_trade_oracle_smoke.py`](../tests/e2e/test_trade_oracle_smoke.py).
Asks: *does Gecko's verdict actually diverge from a one-shot Tavily
answer on the same Solana-DeFi question?* If unique-host count and
dissent rate are both ~0, the KaaS-oracle thesis is decorative
(Pattern D in `CLAUDE.md`).

### Run

```bash
GECKO_E2E_BASE_URL=https://api.geckovision.tech \
TAVILY_API_KEY=tvly-... \
  uv run python scripts/falsifier_tavily.py
```

Or quick iteration with fewer questions:

```bash
GECKO_E2E_BASE_URL=http://localhost:8000 \
TAVILY_API_KEY=tvly-... \
  uv run python scripts/falsifier_tavily.py --limit 2
```

Outputs:

- A markdown report to stdout (aggregate + per-question).
- Raw JSON to `docs/superpowers/falsifier-results/trading_oracle_vs_tavily.json`
  — gitignored; manual judging artifact, not a CI input.

### Reading the result

- **`Hosts unique to Gecko`** — URLs Gecko cited that did not appear in
  Tavily's top-10. High = the paid-corpus ingest is surfacing material
  Tavily can't.
- **`Rows with dissent`** — count of questions where the verdict
  carries `>=1 blocker_question`. Tavily one-shots cannot produce this
  by construction; >50% dissent rate is the adversarial-debate
  signature.
- Both ~0 across all rows → kill the thesis or refind the wedge.
