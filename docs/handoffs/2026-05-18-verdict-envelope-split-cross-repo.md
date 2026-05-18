# Handoff — verdict envelope split (breaking change) → gecko-claude + gecko-mcpay-app

**Date:** 2026-05-18
**From:** gecko-mcpay-api (S35-#99, merged to main via PR #25)
**To:** gecko-claude (`skill.md`) and gecko-mcpay-app (the demo)

## What changed

S35-#99 split the `gecko_trade_research` verdict envelope. The single
`citations[]` field is **gone**, replaced by two top-level lists:

- `evidence_citations[]` — protocol/market-data chunks (`protocol_native`,
  `market_data`, `paysh_live`, `bazaar_live`). "The data."
- `framework_context[]` — investor-canon chunks (`canon_*`). "The lens."

Both carry the same per-item `Citation` shape (provider_kind, snippet, url,
…); ids are 1-indexed *within each list*.

This is a **breaking change** to the `gecko_trade_research` response. There is
no `citations` alias — a consumer reading `verdict.citations` gets nothing.

## Why

The N=30 ship-gate proved `citation_relevance` and `provider_kind_coverage`
were anti-correlated while canon and protocol data shared one list. Splitting
them decoupled the metrics; verified result: `citation_relevance` 0.468 →
0.703. Detail: `docs/eval/2026-05-18-s35-verification-shipgate.md`.

## What each repo must update

### gecko-claude — `skill.md`
Any example or schema in `skill.md` that shows the `gecko_trade_research`
response with a `citations` array must be updated to the two fields. The
verdict example should show `evidence_citations` + `framework_context`
separately — and this is a *feature* to surface, not just a rename: the skill
can now tell the user "here is the on-chain data, and here is the investor-
canon lens" as distinct sections.

### gecko-mcpay-app — the demo
The demo route renders a verdict. Wherever it reads `verdict.citations`,
switch to the two lists. This is a UX opportunity: render evidence and
framework as two visually distinct sections (the product-design intent behind
choosing the two-field split over a role tag).

## Coordination

- The API side is merged (`main`). The `/openapi.json` contract reflects the
  new shape now.
- Until each consumer is updated, its verdict rendering shows empty/missing
  citations — degrades visibly, does not crash.
- No rush gate: `X402_MODE` is still `stub`, pre-real-users — but update
  before the next demo or any external walkthrough.
