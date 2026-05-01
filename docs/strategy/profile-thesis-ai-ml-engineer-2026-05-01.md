# Profile Thesis — AI/ML Engineer Lens

**Date:** 2026-05-01
**Verdict:** Refine. The "orchestration + context" wedge is real but underspecified; without the adversarial debate output as the load-bearing artifact, we lose to Perplexity on day one.

## 1. Is "orchestration + context" defensible?

Not by itself. Perplexity orchestrates retrieval + synthesis. ChatGPT search does too. Bazaar's discovery is a single-shot ranker. What none of them do — and what Gecko already does — is **adversarial multi-agent debate that produces a structured verdict (KILL/REFINE/BUILD) with grounded dissent.** The orchestrator's actual job is not "pull the right docs," it's "stage a debate where analyst, critic, architect, scoper, and judge cite different profile types and disagree on shippability." The wedge is the *shape of the output* (verdict + dissent + scoper-bounded scope), not retrieval breadth.

Defensibility against Perplexity = the eval harness. We grade verdicts against expected outcomes on holdout-live; they grade chat helpfulness. Different contracts, different products. Hold the line: Gecko emits judgments, not summaries.

## 2. Does the 5-voice debate evolve?

**Hybrid.** The 5 internal advisor voices (CEO/CTO/BM/PM/SM) stay as Gecko-owned synthesis personas — they are deterministic role-shaped reasoning, not crowd opinion. Cited external profiles feed RAG and *populate the dissent surface*: the analyst persona opens with "Judge 0xABC (47 cited BUILDs in DeFi, 80% shipped) argues X; investor 0xDEF (22 cited KILLs) argues Y." Reputation weights the *citation prominence in prompt context*, not the persona vote.

New v5.x prompt work needed: (a) citation-formatting block that injects `(profile_type, reputation_score, prior_verdict_outcomes)` into analyst/critic context; (b) judge rubric update so dissent grounded in high-reputation cited profiles is scored higher than ungrounded dissent. No new persona. Adding a sixth voice dilutes the eval contract and breaks v5.x baselines.

## 3. Routing logic — model surface or heuristic?

Heuristic first, model second. The existing `gecko_core.classify` already returns `(category, vertical, gap_classification)`. Extend with a deterministic profile-type map: `vertical=defi → [judge, investor, on_chain_analyst]`, `vertical=consumer → [pm, designer, founder]`. Ship that as a lookup table in Sprint 15. Only escalate to a profile-router LLM call when the heuristic misclassifies ≥20% on a fixture suite — and that suite has to exist before the router does. Reverse order = vibes-driven model adoption.

## 4. RAG quality at 1M chunks

Hybrid scoring, weighted log-linear: `score = α·cosine_sim + β·log(1 + cited_count) + γ·verdict_outcome_rate + δ·recency_decay`. Cosine alone collapses at 1M chunks because every idea has 10k near-neighbors. Cited-count is the trust signal that breaks ties. `verdict_outcome_rate` (cited in BUILDs that shipped) is what makes Gecko's index different from a vector DB — it's a *forward-grade* on the contributor.

Coefficients (α, β, γ, δ) are eval-tunable, not hand-set. Build a profile-RAG fixture suite (200 ideas with known good/bad cited profiles) before tuning. At 1M chunks, also need a two-stage retriever (BM25 prefilter → dense rerank) — pure pgvector ANN gets slow past ~500k.

## 5. Sprint 15 ticket

**S15-AIML-01: Profile-aware RAG scoring + fixture suite (4d)**

- Add `cited_count` and `verdict_outcome_rate` columns to chunks table (coordinate w/ data-engineer).
- Implement hybrid scorer behind `RAG_PROFILE_WEIGHTS` env flag, default off.
- Author 50-fixture `profile_rag` suite: each fixture pins (idea, expected_top_3_profile_types, expected_cited_authors).
- Acceptance: stub-mode eval shows ≥0.70 top-3 profile-type match; flag-on vs flag-off A/B run logged; no holdout regression (≥0.80 retained).
- Use OpenRouter; no live gate. Document weights as v0, not tuned.
