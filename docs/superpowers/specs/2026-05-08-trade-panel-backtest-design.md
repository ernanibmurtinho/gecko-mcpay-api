# Phase 9 — Trade-Panel Backtest — Design

**Date:** 2026-05-08
**Status:** Spec — pending user input on data sources before implementation
**Owner:** ernanibmurtinho
**Predecessor:** Phase 8 (`a4523d8` + `d482a6f` — 7-agent trade panel sync v1)

## 1. Why this is a spec, not an implementation

Phase 8 shipped the `gecko_trade_research` tool with the Strategist agent currently producing **forward-looking intent only** (action / size / horizon). Backtest was deferred from Phase 8 because it requires data-source decisions the founder hasn't picked yet. Implementing autonomously without those picks would lock us into a vendor that doesn't match the rest of the corpus.

Three open decisions block implementation. This doc captures the design assuming each can be answered cleanly.

## 2. What "backtest" means here

Given the Strategist's intent (e.g. "long Jito ~1% size, 14d horizon, stop at recent oracle staleness band"), the backtest answers:

- **What would this trade have done if executed at intent-time, T-30d, T-90d?**
- **What's the realized PnL distribution across N similar historical setups?**
- **What's the worst drawdown? What's the median outcome?**
- **What's the trade's edge vs. a random buy-and-hold of the same protocol?**

Output: a `BacktestReport` model attached to `TradePanelVerdict.backtest`. Nothing in the panel decision changes today — Strategist still emits the intent first; backtest annotates it with realized-historical context.

## 3. Open decisions (need founder input)

### D1 — Price/oracle history source

| Option | Cost | Coverage | Wedge fit |
|---|---|---|---|
| **CoinGecko Pro** | $129/mo unlimited | Top 5000 tokens, 1m granularity | Already in Bazaar catalog at $0.01/call |
| **Birdeye Pro** | $99/mo | Solana-native, every SPL pair | Solana-DeFi-native; not in Bazaar |
| **Pyth Hermes** | Free public RPC | Real-time + historical | Solana-native; the protocol our Pyth chunks describe |
| **DefiLlama OSS API** | Free | Aggregate TVL only, no per-token price | TVL-only |

**Recommendation:** Pyth Hermes for spot prices on Solana protocols. Free, native, matches our Pyth corpus chunks. Fallback to CoinGecko for cross-chain or pre-Pyth-listing periods.

### D2 — Position-simulator semantics

| Option | What it accounts for | Complexity |
|---|---|---|
| **Naive PnL** | Entry price → exit price, hold period only | Trivial. Ships in Phase 9 v1. |
| **Realistic PnL** | Slippage model, fee schedule, oracle staleness, MEV | 5-day rabbit hole. Phase 9.5 if v1 lands. |
| **Full backtester** | Position sizing rules, stop-loss execution, partial fills | 10+ days. Phase 10 if ever. |

**Recommendation:** Naive PnL for v1. Document the limitation clearly in `BacktestReport`. Realistic PnL only after we see if traders actually use the backtest output.

### D3 — Historical depth & frequency

| Window | What it answers | Storage cost (Mongo Atlas) |
|---|---|---|
| 30d, 1h granularity | Recent regime only | ~720 candles/protocol × 5 protocols = 3.6k docs |
| 1y, 1d granularity | Bull/bear cycle context | ~365 × 5 = 1.8k docs |
| 1y, 1h granularity | Full intra-day depth | 8.7k × 5 = 43k docs |
| 5y, 1d granularity | Full crypto cycle | ~1825 × 5 = 9.1k docs |

**Recommendation:** 1y daily + 30d hourly. ~44k Mongo docs total. Re-ingest daily via cron.

## 4. Architecture (assuming D1=Pyth, D2=Naive, D3=1y+30d)

```
packages/gecko-core/src/gecko_core/orchestration/trade_panel/
  backtest/
    __init__.py           # public surface: backtest_intent(intent, history) -> BacktestReport
    history_source.py     # Pyth Hermes adapter; falls back to CoinGecko via existing rag_query path
    simulator.py          # Naive PnL: entry @ T0 mid, exit @ T0+horizon mid; stop-loss check at daily granularity
    models.py             # BacktestReport: pnl_pct, drawdown_pct, n_similar_setups, hit_rate, ...
    storage.py            # Mongo writer for time-series; idempotent upserts on (protocol, timestamp)

packages/gecko-core/src/gecko_core/orchestration/trade_panel/__init__.py
  # Modify: optionally call backtest_intent after Strategist emits intent;
  # attach result to TradePanelVerdict.backtest. Behind a flag --enable-backtest
  # (CLI / kwarg) so v1 trade panel callers don't pay the latency by default.
```

### Strategist prompt change

Add to `_default_prompts.json` `strategist`:

> "Your intent payload SHOULD be backtestable: emit `entry_window` (e.g. 'today, 24h'), `exit_horizon` (e.g. '14d'), `direction` ('long'|'short'|'neutral'), `size_band` ('1%'|'3%'|'5%'). The system will replay this intent against historical price data and attach a backtest report."

### New CLI flag

`gecko_trade_research --enable-backtest` (and `tier=pro` enables it by default; `tier=basic` keeps it off for cost). The MCP tool surface: `tier="pro"` adds `backtest` to the response.

## 5. Cost reality

Per-call extra latency: ~500ms for Pyth fetch (cached) + ~50ms simulation. Acceptable.

Per-call extra spend: $0 (Pyth Hermes is free public RPC).

Storage: ~44k docs at ~200 bytes each = ~9MB. Negligible vs the existing chunks collection.

## 6. Tests

Light fakes per `feedback_lighter_tests`:

- `test_backtest_naive_pnl_long_position_winner` — entry $100, exit $115, 14d horizon → +15% PnL.
- `test_backtest_short_position_loser` — entry $100, exit $115, short → -15%.
- `test_stop_loss_triggers_at_daily_granularity` — entry $100, stop $90, intraday low $89 (next day) → stop triggers, exit at $90.
- `test_history_missing_returns_unbacktestable` — Pyth has no data for `protocol`, returns `BacktestReport(unbacktestable=True, reason="no_history")`.

NEVER fire real Pyth requests in tests. Mock `history_source` with canned candles.

## 7. Schema delta

New collection `protocol_price_history`:

```javascript
{
  protocol: "kamino",   // matches the existing chunks.protocol[] tag
  ts: ISODate(...),
  granularity: "1d",    // "1h" | "1d"
  source: "pyth",       // "pyth" | "coingecko" | "fallback"
  open: 0.0,
  high: 0.0,
  low: 0.0,
  close: 0.0,
  vol_usd: 0.0,
}
```

Index: `{protocol: 1, granularity: 1, ts: -1}` (compound).

## 8. Out of scope (v1)

- Realistic slippage / fee modeling. Phase 9.5.
- Partial fill / liquidity ladder. Phase 10.
- Forward-walk / cross-validation across N protocols. Phase 10.
- Comparison vs random/buy-and-hold baseline. Phase 9.5.
- Per-user wallet history (treat backtest as protocol-level signal, not per-user).

## 9. Falsifier

If after Phase 9 ships, **0 of 5 trading-skill builders cite the backtest output** as a decision input over 4 weeks, the feature is dead weight. Either:

- Remove backtest entirely (revert to intent-only).
- Or pivot to a different shape (e.g., regime-classification rather than PnL-replay).

Track: % of `gecko_trade_research` calls that pass `tier=pro` AND surface `backtest` in the partner skill UI.

## 10. Cross-repo

- This repo: `orchestration/trade_panel/backtest/` package + tests.
- `gecko-mcpay-app`: optional UI to render the backtest panel (Phase 9.5 if we go web).
- `gecko-claude`: update `examples/trading-oracle/skill.md` to mention `tier=pro` unlocks backtest.

## 11. Decision required from founder before implementing

Three multiple-choice answers:

1. **Price source** = Pyth Hermes (recommended) | CoinGecko Pro | Birdeye Pro | hybrid
2. **Simulator depth** = Naive PnL (recommended) | Realistic | Full backtester
3. **Window** = 1y daily + 30d hourly (recommended) | 5y daily | 30d hourly only | other

Reply with three choices or "all defaults" and Phase 9 implementation can be planned + dispatched.
