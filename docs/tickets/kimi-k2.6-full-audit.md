# TICKET: kimi-k2.6-full-audit

**Date:** 2026-05-05  
**Status:** Open — investigation required  
**Severity:** P1 — `OrchestrationError: LLM returned empty content` blocks every pro-tier session  
**Depends on:** `docs/tickets/openrouter-agent-deep-analysis.md`

---

## Problem

`moonshotai/kimi-k2.6` is a reasoning model. When called with
`response_format={"type": "json_object"}` via OpenRouter, its internal thinking
trace exhausts `max_tokens` before emitting any visible output → OpenRouter
returns HTTP 200 with `content=null` → `OrchestrationError: LLM returned empty
content`.

We patched `catalog.py` line 246 (`general_reasoning, balanced` → DeepSeek V3.2)
in 0.2.7, but the error persists. The reason: **we only changed one of many
places that reference Kimi K2.6**. There are references in `model_catalog.json`
with routing-flavoured fields (`best_value`, `default_model`, `budget`) that have
not been audited.

---

## Known References (from user grep)

```
packages/gecko-core/src/gecko_core/routing/model_catalog.json
  "kimi-k2.6": { ... }                       ← catalog entry itself
  "best_value": "kimi-k2.6"                  ← appears multiple times (which task profiles?)
  "best_value": "kimi-k2.6"
  "budget": "kimi-k2.6"
  "best_value": "kimi-k2.6"
  "best_value": "kimi-k2.6"
  "best_value": "kimi-k2.6"
  "default_model": "kimi-k2.6"
  "models": ["claude-sonnet-4.6", "deepseek-v4-pro", "kimi-k2.6"]

packages/gecko-core/src/gecko_core/routing/catalog.py
  "Kimi K2.6": "moonshotai/kimi-k2.6"        ← _NAME_TO_ID resolver (needed)
  (general_reasoning, balanced) → Kimi K2.6  ← PATCHED to DeepSeek V3.2 in 0.2.7
  (complex_coding, balanced)    → Kimi K2.6  ← architect AG2 voice (plain text, likely safe)
  (planning, balanced)          → Kimi K2.6  ← judge AG2 voice (plain text, likely safe)
  (creative_writing, balanced)  → Kimi K2.6  ← refiner/judge_synth (are these invoked?)
  (tool_calling, balanced)      → Kimi K2.6  ← what role uses this?

packages/gecko-mcp/tests/test_advisor_tools.py
  "model_used": "moonshotai/kimi-k2.6"       ← test fixture (may be stale)
```

---

## Investigation Required

### Q1: What do the routing fields in `model_catalog.json` actually do?

`model_catalog.json` has fields like `best_value`, `default_model`, `budget`,
`models` on task-profile entries. `ModelEntry` in `catalog.py` uses
`model_config = ConfigDict(extra="allow")` — unknown fields are stored but not
typed.

**Does any code path read these extra fields for routing decisions?**
Search for: `.extra`, `entry.model_extra`, `catalog["kimi-k2.6"]`,
`"default_model"`, `"best_value"` anywhere that reads from a `ModelEntry`.

### Q2: Which `catalog.py` matrix cells with Kimi K2.6 are called with `json_object`?

Every call site that uses `_call_llm` or `_call_json` with a model resolved from
the matrix is a potential failure point. The AG2 debate voices use plain text
(safe). The synthesis / post-processing paths use json_object (dangerous).

Map each remaining Kimi K2.6 matrix cell to its call site and determine whether
it reaches a `json_object`-constrained `AsyncOpenAI` call.

### Q3: Are `refiner` and `judge_synth` roles active?

`settings.py` defines `max_tokens_refiner` and `max_tokens_judge_synth`. Are
these roles invoked anywhere in `workflows.py`, `gecko_mcp`, or `gecko_api`?
If yes, they use `(creative_writing, balanced)` → Kimi K2.6 + json_object →
same failure mode.

### Q4: Is the `model_catalog.json` `default_model` field used at runtime?

If yes, what code path reads it? Is it ever used as a fallback when
`_TASK_TIER_TO_MODEL_ID` doesn't have a matching cell?

---

## Expected Output

A complete list of:
1. Every active code path that resolves to Kimi K2.6 at runtime
2. For each path: whether it uses `json_object` mode (dangerous) or plain text (safe)
3. Required changes to eliminate Kimi K2.6 from ALL json_object paths
4. Confirmation that remaining Kimi K2.6 usage (AG2 plain-text voices) is safe

---

## Delegate to

`software-engineer` — file-level audit + fixes  
`ai-ml-engineer` — confirm safe/unsafe classification for each path

---

## Findings (2026-05-05 — S22-KIMI-AUDIT)

### Q1: Are `model_catalog.json` routing fields live code?

**No. Dead metadata.** The `task_routing`, `user_priority_presets`, and `cost_tiers` sections of `model_catalog.json` are never read at runtime. `load_catalog()` only parses the top-level `models` dict into typed `ModelEntry` objects. `ModelEntry` uses `extra="allow"` (pydantic stores unknown fields in `model_extra`) but no Python code anywhere in `packages/` or `apps/` reads `.model_extra["best_value"]`, `.model_extra["default_model"]`, or `.model_extra["budget"]` from a `ModelEntry`. These fields are documentation/research metadata only. No routing change required in the JSON.

### Q2: Which matrix cells resolved to Kimi K2.6 and what do they do?

Full inventory of cells that resolved to Kimi K2.6 before this fix:

| Cell | Role(s) | Call site | Mode | Status |
|---|---|---|---|---|
| `general_reasoning × balanced` | `research_basic`, `critic` | `basic.py:generate()` → `_call_llm` | `json_object` | **Fixed in 0.2.7** (→ DeepSeek V3.2) |
| `creative_writing × balanced` | `refiner`, `product_manager` | `refine.py:refine_idea()` → `json_object` | `json_object` | **Fixed in 0.2.8** (→ DeepSeek V3.2) |
| `tool_calling × balanced` | (none currently) | latent risk if a future role maps here | would be `json_object` | **Fixed in 0.2.8** (→ DeepSeek V3.2) |
| `complex_coding × balanced` | `architect`, `cto` | AG2 `a_generate_reply` | plain text | **SAFE — left as Kimi K2.6** |
| `planning × balanced` | `judge`, `ceo` | AG2 `a_generate_reply` | plain text | **SAFE — left as Kimi K2.6** |
| `code_review × budget` | (none — no role maps here) | — | — | Out of scope (no role maps to `code_review`) |

The `critic` role maps to `general_reasoning` (patched in 0.2.7). The AG2 plain-text voices (`architect`, `judge`, `ceo`, `cto`) call `a_generate_reply` which produces prose — no `response_format=json_object` constraint — so Kimi K2.6's internal reasoning trace is fine; the full token budget is available for visible output.

### Q3: Are `refiner` and `judge_synth` roles active?

- **`AgentRole.refiner`** — ACTIVE. `refine.py:refine_idea()` is called by `bb refine <hash>` (CLI). Uses `response_format=json_object` (via `build_response_format`). Resolved to Kimi K2.6 at `creative_writing × balanced` → **this was the second active failure path**. Fixed in 0.2.8.

- **`AgentRole.judge_synth`** — ACTIVE but already safe. `judges/synth.py:synthesise_judge_skill_md()` is called by `bb judges synth`. Uses `_SYNTH_TIER = Tier.quality` → `creative_writing × quality` → **Claude Sonnet 4.6** (Anthropic, non-reasoning). Never resolved to Kimi K2.6.

### Q4: `_VOICE_TIMEOUT_SECONDS` value

`_VOICE_TIMEOUT_SECONDS = 60.0` (line 68 of `pro/__init__.py`). The server is running the patched 60s value. The 15s value was pre-S12-LATENCY-01.

---

## Changes Applied (0.2.8)

### `packages/gecko-core/src/gecko_core/routing/catalog.py`

1. `(TaskProfile.creative_writing, Tier.balanced)`: `Kimi K2.6` → `DeepSeek V3.2`
   - Eliminates the `refiner` json_object failure path (`bb refine` → `refine.py`).
   - Also removes `product_manager` (Advisor Panel, currently inactive) from the Kimi path.

2. `(TaskProfile.tool_calling, Tier.balanced)`: `Kimi K2.6` → `DeepSeek V3.2`
   - Preventive: no current `AgentRole` maps to `tool_calling`, but the cell existed as a latent risk if a future role were mapped here.

### `packages/gecko-core/tests/routing/test_catalog.py`

- `test_refiner_role_resolves_to_creative_writing_balanced`: updated assertion from `moonshotai/kimi-k2.6` → `deepseek/deepseek-v3.2` with explanatory docstring.
- `test_research_basic_role_resolves_to_general_reasoning_balanced`: updated assertion (was already patched in catalog.py 0.2.7 but test was stale) from `moonshotai/kimi-k2.6` → `deepseek/deepseek-v3.2` with explanatory docstring.

### Version bumps

- `packages/gecko-core/pyproject.toml`: `0.2.7` → `0.2.8`
- `packages/gecko-mcp/pyproject.toml`: `0.2.7` → `0.2.8`; `gecko-core>=0.2.7` → `gecko-core>=0.2.8`

### Lint / tests

- `ruff check --fix` + `ruff format --check`: clean (one pre-existing unused import in `workflows.py` fixed as collateral)
- `pytest packages/gecko-core/tests/routing/test_catalog.py`: 18/18 passed
