#!/usr/bin/env bash
# S2X-15 — Mainnet cutover eval gate.
#
# Runs the three Pro-tier sub-suites (general, crypto, saas) in --live mode
# against prompts v5 + V1 sources. Each sub-suite must achieve
# verdict_accuracy >= 0.85. Exits 0 only if ALL three pass.
#
# This script costs real money (~$95 in OpenAI tokens + ~$15 Anthropic
# rubric tokens at --reruns 3). It is interactive by design: you must type
# 'y' to proceed. `--reruns 3` runs each idea three times and the harness
# takes the majority verdict, dampening single-sample variance that
# dominated the Apr-28 v5.x runs (see tests/eval/live_runs/).
#
# Usage:
#   ./scripts/run_eval_gate.sh
#
# See: docs/runbooks/eval-gate.md
#       docs/decisions/0001-mainnet-after-v1-sources.md

set -euo pipefail

PASS_THRESHOLD="0.85"
SUITES=(general crypto saas)
RERUNS="${GECKO_EVAL_RERUNS:-3}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE_RUNS_DIR="${ROOT_DIR}/tests/eval/live_runs"

cd "${ROOT_DIR}"

# --- 1. Env preconditions ----------------------------------------------------

: "${OPENAI_API_KEY:?OPENAI_API_KEY must be set (5 AG2 agents need it)}"

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_API_KEY:-}" ]; then
  echo "ERROR: ANTHROPIC_API_KEY (or CLAUDE_API_KEY) must be set for the Sonnet 4.6 rubric judge." >&2
  exit 2
fi

GECKO_API_BASE="${GECKO_API_BASE:-https://api.geckovision.tech}"
export GECKO_API_BASE

# --- 2. Repo state preconditions --------------------------------------------

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "${CURRENT_BRANCH}" != "main" ]; then
  echo "ERROR: must be on 'main' branch (currently on '${CURRENT_BRANCH}')" >&2
  exit 2
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: working tree is dirty. Commit or stash before running the gate." >&2
  exit 2
fi

# --- 3. Spend confirmation ---------------------------------------------------

cat <<EOF

================================================================
  S2X-15 — Mainnet cutover eval gate
================================================================

  Suites:        general (20) + crypto (15) + saas (15) = 50 ideas
  Reruns/idea:   ${RERUNS} (override via GECKO_EVAL_RERUNS env var)
  Mode:          --live (real AG2 debate + Sonnet 4.6 rubric)
  API base:      ${GECKO_API_BASE}
  Pass bar:      verdict_accuracy >= ${PASS_THRESHOLD} per sub-suite
  Threshold ADR: docs/decisions/0001-mainnet-after-v1-sources.md

  Expected spend (at --reruns ${RERUNS})
    OpenAI:       ~50 * ${RERUNS} * \$0.60 = \$$(python3 -c "print(round(50 * ${RERUNS} * 0.60, 2))")
                  (Judge on gpt-4o; Analyst/Critic/Architect/Scoper on gpt-4o-mini)
    Anthropic:    ~50 * ${RERUNS} * \$0.10 = \$$(python3 -c "print(round(50 * ${RERUNS} * 0.10, 2))")
                  (Sonnet 4.6 rubric, one call per idea-run)
    TOTAL:        ~\$$(python3 -c "print(round(50 * ${RERUNS} * 0.70, 2))")

  Expected runtime: ~$(( 30 * ${RERUNS} ))-$(( 45 * ${RERUNS} )) minutes sequential.

EOF

read -r -p "Proceed? Type 'y' to continue, anything else aborts: " CONFIRM
if [ "${CONFIRM}" != "y" ] && [ "${CONFIRM}" != "Y" ]; then
  echo "Aborted."
  exit 1
fi

# --- 4. Run each suite sequentially -----------------------------------------

declare -A SUITE_ACCURACY
declare -A SUITE_RUNFILE

today="$(date -u +%Y-%m-%d)"

for suite in "${SUITES[@]}"; do
  echo
  echo "=== [${suite}] running --live --reruns ${RERUNS} ==="
  uv run python -m tests.eval.runner --suite "${suite}" --live --reruns "${RERUNS}"

  # Pick the newest matching live run JSON for this suite + date.
  # Pattern: ${today}-${suite}.json or ${today}-${suite}-N.json (newest wins).
  runfile="$(ls -1t "${LIVE_RUNS_DIR}/${today}-${suite}"*.json 2>/dev/null | head -n 1 || true)"
  if [ -z "${runfile}" ]; then
    echo "ERROR: no live-run JSON found for suite=${suite} date=${today} under ${LIVE_RUNS_DIR}" >&2
    exit 3
  fi

  acc="$(python3 -c "import json,sys; print(json.load(open('${runfile}'))['aggregate']['verdict_accuracy'])")"
  SUITE_ACCURACY["${suite}"]="${acc}"
  SUITE_RUNFILE["${suite}"]="${runfile}"
  echo "[${suite}] verdict_accuracy=${acc}  (${runfile})"
done

# --- 5. Tabulate + decide ---------------------------------------------------

echo
echo "================================================================"
echo "  S2X-15 — Eval gate results"
echo "================================================================"
printf "  %-10s %-12s %-8s %s\n" "suite" "verdict_acc" "verdict" "run_file"
echo "  ----------------------------------------------------------------"

ALL_PASS=1
for suite in "${SUITES[@]}"; do
  acc="${SUITE_ACCURACY[${suite}]}"
  runfile="${SUITE_RUNFILE[${suite}]}"
  pass="$(python3 -c "print('PASS' if float('${acc}') >= float('${PASS_THRESHOLD}') else 'FAIL')")"
  if [ "${pass}" = "FAIL" ]; then
    ALL_PASS=0
  fi
  printf "  %-10s %-12s %-8s %s\n" "${suite}" "${acc}" "${pass}" "$(basename "${runfile}")"
done

echo
if [ "${ALL_PASS}" -eq 1 ]; then
  cat <<'EOF'
S2X-15 GATE: PASS
  All three sub-suites cleared verdict_accuracy >= 0.85.
  Notify web3-engineer to begin mainnet cutover per
  docs/runbooks/mainnet-cutover.md.
EOF
  exit 0
else
  cat <<'EOF'
S2X-15 GATE: FAIL
  At least one sub-suite is below 0.85. Mainnet cutover is BLOCKED.
  Recovery options (see docs/runbooks/eval-gate.md §"On failure"):
    1. Roll prompts back: export GECKO_PRO_PROMPTS_VERSION=v4
    2. File a prompt-rework follow-up; do NOT proceed to mainnet.
EOF
  exit 1
fi
