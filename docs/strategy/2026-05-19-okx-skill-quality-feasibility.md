# OKX Skill Quality Award — Feasibility Diagnostic (2026-05-19)

**Decision needed:** Commit ~2 days to a Skill Quality Award submission?
**Window:** closes 2026-05-21 ~10:00 UTC (May 21 18:00 UTC+8). ~2 working days left.
**Mode:** read-only diagnostic, no code, no spend.

## Headline

**NO-GO** as a fresh ~2-day build. **CONDITIONAL-GO** only if scope is cut to a
*thin fork of `starter-coach`* with `gecko_trade_research` wired at exactly one
decision point. Honest read below.

---

## Q1 — Hard-requirement satisfiability

**Gate (verbatim):** *"Skill must use onchainOS as the primary data source and
trading tool."*

What "primary data source and trading tool" means in practice, from the
installed examples:

- `starter-coach/SKILL.md` is explicit: *"On-chain data backbone: OnchainOS CLI
  (sole source for smart_money, dev, bundler, fresh_wallet, honeypot, lp_locked,
  taxes, top_holders tags)."* Step 3 makes a live OnchainOS verification call
  **mandatory** before any strategy card renders. Step 5 routes all execution
  through `onchainos swap execute`.
- The 20 `okx-*` skills are all thin CLI wrappers — `okx-dex-*`, `okx-security`,
  `okx-audit-log`, `okx-onchain-gateway`. Every on-chain *read* and every
  *trade* goes through the `onchainos` binary. There is no example skill that
  sources market data from anywhere else.

**Interpretation (this is the gate, honest read):** the requirement constrains
the **data feed** (prices, candles, holders, safety tags) and the **execution
venue** (swaps). It does NOT forbid an external *reasoning/decision* layer.
`starter-coach` itself already has an external decision source — `llm_strategy.py`
calls an LLM to generate the strategy spec. OnchainOS is still "primary" because
every fact and every trade flows through it; the LLM only *reasons over* OnchainOS
data.

**Therefore a Gecko skill qualifies IF and ONLY IF:**
- all market data, safety tags, candles, balances come from `onchainos` calls;
- all swaps execute via `onchainos swap execute`;
- `gecko_trade_research` is positioned as a **verdict/decision overlay** that
  consumes OnchainOS-sourced facts — never as a data source competing with it.

A bare `gecko_trade_research`-wrapper skill (Gecko corpus + Gecko price data, no
OnchainOS) **fails the gate outright.** The architecture that passes is
narrow: fork `starter-coach`, keep its entire OnchainOS spine, insert one Gecko
verdict call. The hard requirement is satisfiable but it dictates the design —
we do not get to ship the oracle-first shape here.

## Q2 — Is `gecko_trade_research` cleanly callable from inside the skill?

`gecko_trade_research` exists as (a) an MCP tool (`gecko-mcp/server.py:703`,
dispatch at `:922`) and (b) the x402-paid `api.geckovision.tech/trade_research`
endpoint. `_run_trade_research` forwards to the remote API when `GECKO_API_URL`
is set.

OKX skills execute as **Python invoked by the agent** (`coach.py` shells out to
the `onchainos` CLI via `subprocess`; see `onchainos.py:_run_cli`). There is no
MCP-to-MCP brokering inside a skill — a skill is Python + a CLI binary.

**Cleanest integration path: a plain HTTPS call** from the skill's Python to
`https://api.geckovision.tech/trade_research` (mirror the `onchainos.py` wrapper
pattern — a small `gecko_oracle.py` with an `httpx`/`urllib` POST). This is
low-friction:
- The endpoint is already live and returns the verdict envelope (verdict,
  confidence, surviving_dissent, citations).
- `X402_MODE=stub` in production means the call settles for **$0** today — no
  buyer wallet, no 402 handshake. Judges running the skill pay nothing.
- It is one new ~60-line file plus one call site in the coach flow.

Friction / risks:
- Stub mode means the x402 *payment* path is NOT exercised — fine for a Skill
  Quality artifact (judged on quality, not settlement), but the skill must not
  *claim* live x402 settlement in copy.
- The skill now has a hard runtime dependency on `api.geckovision.tech` being up
  while a judge runs it. Needs a graceful fallback ("oracle unavailable, using
  baseline") so a transient 500 doesn't brick the human-score run.
- MCP-to-MCP is NOT a viable path inside the OKX skill model — do not attempt it.

**Verdict:** callable, clean, ~1 file. Not the blocker.

## Q3 — Realistic 2-day scope

The blocker is **calendar + quality bar**, not feasibility.

Reality check on `starter-coach`: it is **6,875 lines** across coach.py (2,359),
onchainos.py (1,061), schema.json (865), SKILL.md (961), plus engines. It is a
mature, polished artifact authored over weeks. The Skill Quality Award is
**50% AI score + 50% human judges running it** — originality, executability,
result quality. A rushed fork competes head-on with that.

**What 2 days can credibly produce (the conditional-GO scope):**
- Fork `starter-coach` wholesale → `gecko-trade-coach`. Keep the OnchainOS
  spine, schema, primitives, paper gate, backtest engine **unchanged** (this is
  what satisfies the hard requirement, and it is already battle-tested).
- Add `gecko_oracle.py` — HTTPS wrapper for `/trade_research` (stub-mode, $0).
- Insert ONE verdict call in Step 3, after `generate_strategy_spec()`: before
  rendering the strategy card, call the oracle and surface
  verdict + confidence + one surviving-dissent line + citations. This is the
  originality wedge — `starter-coach` has no grounded verdict, no dissent, no
  citations.
- Rewrite `SKILL.md` frontmatter, `plugin.yaml`, `README`, `LICENSE`, author
  block. Add a "decision layer" section documenting the Gecko overlay.
- Smoke-test end-to-end against the live stub API + a real `onchainos` call.

**What must be CUT:** any new corpus ingest, any backtest-harness changes, any
new primitives, any execution-adapter work, daemon/hot-swap, the publish-portable
registry story. None of that fits.

**Honest risk:** even the conditional-GO scope is tight. S37's tail still needs
finishing. A fork that is 95% someone else's polished skill plus one verdict
call risks reading as derivative to human judges — *originality* is a scored
axis and "we forked the reference skill" is a weak originality story. The
verdict + dissent + citations overlay is genuinely novel, but it is one panel in
a six-step flow the judges have already seen from `starter-coach`. If the
overlay is janky (slow oracle call, ugly citation rendering, no fallback), the
human score drops below the 10th-place cutoff and the submission is worse than
not submitting — it spends 2 days for $0 and a derivative-looking artifact.

## Unknowns that would change the recommendation

1. Does OKX accept a fork of their own reference skill, or is substantial
   originality a hard expectation? (Not stated in the rules we have.)
2. Will `api.geckovision.tech` stay reliably up during the judging window?
   A judge hitting a 500 zeroes the human score.
3. Is S37's tail truly parkable for 2 days, or does it have its own deadline?
4. IP terms — does OKX claim rights on submitted skills? (Unresolved since
   2026-05-11 diagnostic.)

## Recommendation

**NO-GO** on a 2-day commitment **unless** the founder accepts the narrow
conditional-GO scope above AND S37's tail can genuinely yield 2 clear days.

Rationale, recommendation-first: the hard requirement is satisfiable and the
oracle is cleanly callable — neither is the problem. The problem is that a
*quality* award judged 50% by humans rewards a polished, original artifact, and
2 days buys only a thin fork of the reference skill plus one verdict panel. That
can land mid-pack at best; a rushed version lands below the cutoff and is worse
than abstaining. Per `feedback_okx_no_funding_pressure`, this contest carries no
funding pressure and submission is opt-in — there is no cost to skipping.

**If the founder still wants in:** do the conditional-GO scope, treat the forked
skill as a reusable Gecko distribution artifact (it ships via `gecko-claude`
regardless of contest outcome), and decide submit/abstain at end of day 2 based
on whether the verdict overlay demos cleanly. That preserves optionality —
optionality > leaderboard rank.
