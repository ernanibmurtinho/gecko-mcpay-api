# TICKET: openrouter-agent-deep-analysis

**Date:** 2026-05-05  
**Status:** Fix applied (gecko-core 0.2.7)  
**Severity:** P1 — blocks pro-tier users on `balanced` model assignments  
**Reported by:** user (Manus session timeout + OrchestrationError after deploy)

---

## Root Cause

`OrchestrationError: LLM returned empty content` was raised from `basic.py:270`
on the **basic research synthesis pass** — not from the AG2 voice loop.

`AgentRole.research_basic` resolves via `_ROLE_TO_TASK_MATRIX` to
`TaskProfile.general_reasoning`, which at `Tier.balanced` mapped to
`moonshotai/kimi-k2.6`. Kimi K2.6 is a **reasoning-class model** on OpenRouter:
it performs internal thinking computation whose tokens are charged against the
`max_tokens` budget. When `response_format={"type": "json_object"}` is enforced
and the thinking trace consumes the allotted budget, the model returns HTTP 200
with `content=null`. The 2-retry loop in `_call_llm` sent the same parameters
each time and hit the same wall, ultimately raising after 3 failed attempts.

### Why retrying failed silently

The retry loop checked `if content: break` but did not inspect `finish_reason`.
A `finish_reason="length"` stop means token budget exhausted — retrying with
identical params is guaranteed to reproduce the same failure. The loop silently
re-paid for two additional calls and then raised.

### Three separate empty-content paths (not one)

| Location | Error shape | Notes |
|---|---|---|
| `basic.py:270` | `OrchestrationError: LLM returned empty content` | **Root cause path. Fixed.** |
| `pro/__init__.py:347-354` | `_VoiceOutcome(kind="error", ...)` | AG2 voice path — produces a named error turn, not an exception. Guarded. |
| `post_processors.py:113` | `ValueError("post-processor returned empty content")` | Caught by outer except, degrades to None, never propagates. |

The reported error string uniquely identifies the first path.

---

## Exact Model Assignments at `balanced` Tier (before fix)

| Agent / Role | Task profile | Model | Risk |
|---|---|---|---|
| research_basic | general_reasoning | `moonshotai/kimi-k2.6` | **HIGH — reasoning model + json_object = empty content** |
| critic | general_reasoning | `moonshotai/kimi-k2.6` | Low — AG2 plain-text path, guarded |
| judge | planning | `moonshotai/kimi-k2.6` | Low — AG2 plain-text path, guarded |
| architect | complex_coding | `moonshotai/kimi-k2.6` | Low — AG2 plain-text path, guarded |
| analyst | data_analysis | `deepseek/deepseek-v3.2` | No issues reported |
| scoper | classification | `openai/gpt-4.1-nano` | No issues reported |

The AG2 voice agents use Kimi K2.6 for free-text output — that's fine. The
problem was specifically Kimi K2.6 + `json_object` constraint on the basic
synthesis pass.

---

## Fix Applied (gecko-core 0.2.7)

### Fix A — Swap `(general_reasoning, balanced)` to DeepSeek V3.2

**File:** `packages/gecko-core/src/gecko_core/routing/catalog.py:246`

```python
# Before
(TaskProfile.general_reasoning, Tier.balanced): _id("Kimi K2.6"),

# After
(TaskProfile.general_reasoning, Tier.balanced): _id("DeepSeek V3.2"),
```

`deepseek/deepseek-v3.2` is a non-reasoning completion model already used at
`(data_analysis, balanced)` for the analyst agent with no reported issues.
It supports `json_object` mode reliably on OpenRouter.

### Fix B — `finish_reason == "length"` early-break

**File:** `packages/gecko-core/src/gecko_core/orchestration/basic.py:261-280`

Added `finish_reason` inspection after each attempt. When `finish_reason=="length"`,
log a warning and raise immediately — retrying with the same `max_tokens` is
guaranteed to hit the same wall.

---

## Open Questions

1. **`(complex_coding, balanced)` still maps to Kimi K2.6** (architect agent in AG2).
   This is the plain-text AG2 path where empty-content is already gracefully
   handled by the `_VoiceOutcome` guard. Revisit only if architect starts
   producing empty turns at scale.

2. **Does DeepSeek V3.2 reliably support `json_object` on OpenRouter?**
   Yes — the analyst agent has been using it at `(data_analysis, balanced)` with
   no reported failures. It is a non-reasoning completion model and does not
   consume token budget on internal thinking.

3. **Should we suppress Kimi K2.6 reasoning trace for AG2 voices?**
   Optional hardening: pass `extra_body={"reasoning": {"max_tokens": 0}}` to
   suppress the reasoning trace on Kimi K2.6 calls if voice timeouts recur.
   Not required now that basic synthesis no longer uses Kimi.

---

## Related Files

- `packages/gecko-core/src/gecko_core/routing/catalog.py` — matrix cell change (line 246)
- `packages/gecko-core/src/gecko_core/orchestration/basic.py` — finish_reason guard (lines 261-280)
- `packages/gecko-core/src/gecko_core/orchestration/pro/__init__.py` — AG2 voice empty-content guard (lines 347-354, unchanged)
- `packages/gecko-core/src/gecko_core/orchestration/settings.py` — confirms LLM_ROUTER plane separation

## Sources

- OpenRouter Structured Outputs: https://openrouter.ai/docs/guides/features/structured-outputs
- OpenRouter Reasoning Tokens: https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
- Kimi K2.6 on OpenRouter: https://openrouter.ai/moonshotai/kimi-k2.6
- DeepSeek V3.2 on OpenRouter: https://openrouter.ai/deepseek/deepseek-v3.2
- vLLM empty content bug (Kimi K2): https://github.com/vllm-project/vllm/issues/33654
