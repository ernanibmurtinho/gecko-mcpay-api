#!/usr/bin/env bash
# S4-TWITSH-03 — Live-V1-source eval gate.
#
# Variant of `scripts/run_eval_gate.sh`. Runs the holdout-live suite
# (10 ideas, no canned mock_precedents/rag_context) in --live --live-rag
# mode. Each idea triggers a real dispatch_sources() call:
#   - twit.sh on Base mainnet (~$0.05/idea worst case, $0.10 cap)
#   - HN + Reddit (free)
# So the agents see real V1 signal instead of the canned fixtures.
#
# Pass bar: verdict_accuracy >= 0.80 on the holdout-live suite.
# (Lower than the 0.85 cutover gate because real V1 signal is noisier
# than curated fixtures — see docs/runbooks/eval-gate.md §"Live-V1 gate".)
#
# Spend estimate (per gate run):
#   - twit.sh:   10 * $0.05 = ~$0.50 (worst case; usually less w/ cache)
#   - LLM:       10 * $0.20 = ~$2.00 (gpt-4o-mini agents + gpt-4o judge)
#   - Anthropic: 10 * $0.10 = ~$1.00 (Sonnet 4.6 rubric)
#   - TOTAL:     ~$3.50 per run, with --reruns 1 (default)
#
# Usage:
#   ./scripts/run_eval_gate_live.sh
#
# See: docs/runbooks/eval-gate.md (§"Live-V1 gate")

set -euo pipefail

PASS_THRESHOLD="0.80"
SUITE="holdout_live"
RERUNS="${GECKO_EVAL_RERUNS:-1}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE_RUNS_DIR="${ROOT_DIR}/tests/eval/live_runs"

cd "${ROOT_DIR}"

# --- 1. Env preconditions ----------------------------------------------------

: "${OPENAI_API_KEY:?OPENAI_API_KEY must be set (5 AG2 agents need it)}"

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_API_KEY:-}" ]; then
  echo "ERROR: ANTHROPIC_API_KEY (or CLAUDE_API_KEY) must be set for the Sonnet 4.6 rubric judge." >&2
  exit 2
fi

# twit.sh wallet preconditions — without these, --live-rag falls back to
# zero V1 spend and the gate is meaningless. We do NOT auto-enable
# TWITSH_ENABLED here; the user must opt-in explicitly.
: "${TWITSH_ENABLED:?TWITSH_ENABLED must be set to 'true' for the live-V1 gate}"
: "${TWITSH_WALLET_PRIVATE_KEY:?TWITSH_WALLET_PRIVATE_KEY must be set (Base mainnet signer)}"
: "${TWITSH_WALLET_ADDRESS:?TWITSH_WALLET_ADDRESS must be set}"

# S11-F18-01: bypass the 6h Mongo result cache by default for the gate.
# Without this, repeated runs of the same holdout suite get served free
# cached payloads and `v1_sources_cost_usd` reports $0.00 — which is what
# the F18 anomaly on 2026-04-30 turned out to be. Caller can still opt
# back into cache reuse by exporting TWITSH_BYPASS_CACHE=false beforehand.
export TWITSH_BYPASS_CACHE="${TWITSH_BYPASS_CACHE:-true}"

# --- 2. Spend confirmation ---------------------------------------------------

cat <<EOF

================================================================
  S4-TWITSH-03 — Live-V1-source eval gate
================================================================

  Suite:           ${SUITE} (10 ideas, 5 ship + 5 kill)
  Reruns/idea:     ${RERUNS} (override via GECKO_EVAL_RERUNS)
  Mode:            --live --live-rag
  Pass bar:        verdict_accuracy >= ${PASS_THRESHOLD}
  twit.sh wallet:  ${TWITSH_WALLET_ADDRESS}

  Expected spend (at --reruns ${RERUNS})
    twit.sh:    ~10 * \$0.05 = \$0.50  (cap-bounded)
    OpenAI:     ~10 * ${RERUNS} * \$0.20 = \$$(python3 -c "print(round(10 * ${RERUNS} * 0.20, 2))")
    Anthropic:  ~10 * ${RERUNS} * \$0.10 = \$$(python3 -c "print(round(10 * ${RERUNS} * 0.10, 2))")
    TOTAL:      ~\$$(python3 -c "print(round(0.50 + 10 * ${RERUNS} * 0.30, 2))")

  Expected runtime: ~$(( 8 * ${RERUNS} ))-$(( 12 * ${RERUNS} )) minutes sequential.

EOF

read -r -p "Proceed? Type 'y' to continue, anything else aborts: " CONFIRM
if [ "${CONFIRM}" != "y" ] && [ "${CONFIRM}" != "Y" ]; then
  echo "Aborted."
  exit 1
fi

# --- 3. Run the suite --------------------------------------------------------

today="$(date -u +%Y-%m-%d)"

echo
echo "=== [${SUITE}] running --live --live-rag --reruns ${RERUNS} ==="
uv run python -m tests.eval.runner \
  --suite "${SUITE}" \
  --live \
  --live-rag \
  --reruns "${RERUNS}"

# Pick the newest matching live run JSON.
runfile="$(ls -1t "${LIVE_RUNS_DIR}/${today}-${SUITE}"*.json 2>/dev/null | head -n 1 || true)"
if [ -z "${runfile}" ]; then
  echo "ERROR: no live-run JSON found for suite=${SUITE} date=${today} under ${LIVE_RUNS_DIR}" >&2
  exit 3
fi

acc="$(python3 -c "import json; print(json.load(open('${runfile}'))['aggregate']['verdict_accuracy'])")"
v1_total="$(python3 -c "
import json
d = json.load(open('${runfile}'))
ideas = d.get('ideas') or []
print(round(sum(float(i.get('v1_sources_cost_usd', 0.0)) for i in ideas), 4))
")"

# --- 4. Decide ---------------------------------------------------------------

echo
echo "================================================================"
echo "  S4-TWITSH-03 — Live-V1 eval gate result"
echo "================================================================"
printf "  %-22s %s\n" "verdict_accuracy"  "${acc}"
printf "  %-22s \$%s\n" "v1_sources_total"   "${v1_total}"
printf "  %-22s %s\n" "run_file"          "$(basename "${runfile}")"

if python3 -c "import sys; sys.exit(0 if float('${acc}') >= float('${PASS_THRESHOLD}') else 1)"; then
  cat <<EOF

S4-TWITSH-03 GATE: PASS
  verdict_accuracy ${acc} >= ${PASS_THRESHOLD}
  v5.4 prompts hold up against real V1-source signal.
EOF
  exit 0
else
  cat <<EOF

S4-TWITSH-03 GATE: FAIL
  verdict_accuracy ${acc} < ${PASS_THRESHOLD}
  v5.4 prompts may need adjustment for real V1-source noise. Inspect:
    jq '.ideas[] | select(.actual_verdict != .expected_verdict)' ${runfile}
  Track A (prompt-engineer) owns the rework if needed.
EOF
  exit 1
fi
