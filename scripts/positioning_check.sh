#!/usr/bin/env bash
# Gecko stress matrix runner.
#
# Originally S10-POSITION-01 (hardcoded 5-idea array); S14-DOGFOOD-02
# parameterizes it to take an ideas file (one idea per line, blank/#
# comment lines ignored) so an arbitrary set can be re-fired without
# editing the script.
#
# Usage:
#   scripts/positioning_check.sh                          # stub + default ideas file
#   scripts/positioning_check.sh --live                   # mainnet (Track B preflight required)
#   scripts/positioning_check.sh --ideas <file>           # custom ideas file
#   scripts/positioning_check.sh --live --ideas <file>    # both flags compose
#
# Default ideas file resolution (in order):
#   1. --ideas <file>                                    # explicit
#   2. docs/positioning/ideas/<latest>.txt               # newest by mtime
#   3. fallback to the original 5-idea array             # back-compat
#
# Defensive: if `bb plan` errors mid-run (e.g. F17 voice failures from
# Sprint 9), we capture the failure and continue with the remaining ideas.
# The matrix never crashes the script.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${ROOT}/docs/positioning"
RAW_DIR="${OUT_DIR}/raw/2026-04-30"
OUT_FILE="${OUT_DIR}/2026-04-30-gecko-self-research.md"
mkdir -p "${RAW_DIR}"

MODE="stub"
IDEAS_FILE=""

# Simple flag parser — long-form only, --flag value separated by space.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --live) MODE="live"; shift ;;
    --ideas)
      shift
      IDEAS_FILE="${1:-}"
      if [[ -z "${IDEAS_FILE}" ]]; then
        echo "[positioning] --ideas requires a file path" >&2
        exit 2
      fi
      shift
      ;;
    *)
      echo "[positioning] unknown flag: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "${MODE}" == "live" ]]; then
  echo "[positioning] LIVE mode — assumes Track B preflight (scripts/live_preflight.sh) passed."
  export X402_MODE="live"
else
  echo "[positioning] stub mode (default). Pass --live to flip to mainnet."
  export X402_MODE="stub"
fi

# Resolve the ideas file: explicit > latest in docs/positioning/ideas/
# > hardcoded fallback. The fallback preserves the S10 5-idea array so
# pre-S14 invocations keep working without an ideas file present.
if [[ -z "${IDEAS_FILE}" ]]; then
  DEFAULT_IDEAS_DIR="${ROOT}/docs/positioning/ideas"
  if [[ -d "${DEFAULT_IDEAS_DIR}" ]]; then
    IDEAS_FILE="$(ls -1t "${DEFAULT_IDEAS_DIR}"/*.txt 2>/dev/null | head -n1 || true)"
  fi
fi

IDEAS=()
if [[ -n "${IDEAS_FILE}" && -f "${IDEAS_FILE}" ]]; then
  echo "[positioning] reading ideas from ${IDEAS_FILE}"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    # Skip blank lines + shell-style comments.
    [[ -z "${line// }" ]] && continue
    [[ "${line}" =~ ^[[:space:]]*# ]] && continue
    IDEAS+=("${line}")
  done < "${IDEAS_FILE}"
fi

if [[ ${#IDEAS[@]} -eq 0 ]]; then
  echo "[positioning] no ideas file resolved — falling back to default 5-idea array"
  IDEAS=(
    "AI co-founder for indie hackers, x402-paid via MCP inside Claude Code"
    "Builder Bootstrap Platform that lives inside Claude Code"
    "pay-per-use research agent for solo founders, USDC on Solana"
    "adversarial 5-agent debate to kill bad startup ideas before you build them"
    "upstream of Kiro: should-I-build before how-do-I-build"
  )
fi

TOTAL=${#IDEAS[@]}
echo "[positioning] running ${TOTAL} ideas in ${MODE} mode"

# Resolve the bb entrypoint. Prefer uv run from the repo root so we don't
# depend on a global install.
BB() {
  ( cd "${ROOT}" && uv run bb "$@" )
}

# Strip ANSI color codes so grep/sed can match Rich output reliably.
strip_ansi() {
  sed -E 's/\x1B\[[0-9;]*[A-Za-z]//g'
}

# Pull a UUID-shaped session_id from research output. The CLI prints
# `session_id: <uuid>` as the last line. Falls back to scanning all lines.
extract_session_id() {
  local file="$1"
  strip_ansi <"${file}" \
    | grep -oE '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' \
    | tail -n1
}

# Pull the gap_classification + summary from research output. The renderer
# emits a line like `Gap: Partial:segment — <summary>` inside the validation
# panel.
extract_gap() {
  local file="$1"
  strip_ansi <"${file}" \
    | grep -E '^[[:space:]]*Gap:' \
    | head -n1 \
    | sed -E 's/^[[:space:]]*Gap:[[:space:]]*//'
}

# Best-effort verdict heuristic. The current pipeline doesn't print a
# top-line verdict token; we infer from the gap classification:
#   False         -> KILL
#   Full          -> KILL (someone fully covers it)
#   Partial:*     -> REFINE
# When we can't read the gap line at all we mark UNKNOWN and let the
# delta doc flag it.
infer_verdict() {
  local gap_line="$1"
  local cls
  cls="$(echo "${gap_line}" | awk -F'[ —-]' '{print $1}')"
  case "${cls}" in
    False) echo "KILL" ;;
    Full)  echo "KILL" ;;
    Partial:*) echo "REFINE" ;;
    "") echo "UNKNOWN" ;;
    *) echo "REFINE" ;;
  esac
}

# Count cited sources by counting unique [N] entries in the validation panel.
count_sources() {
  local file="$1"
  strip_ansi <"${file}" \
    | grep -oE '^\[[0-9]+\] http' \
    | sort -u \
    | wc -l \
    | tr -d ' '
}

# Pull top-3 source URLs cited (first three numbered citations in the file).
top_sources() {
  local file="$1"
  strip_ansi <"${file}" \
    | grep -oE 'https?://[^[:space:]]+' \
    | awk '!seen[$0]++' \
    | head -n3
}

# Extract the 5 closing lines from `bb plan` table output. We look for the
# rendered Rich table rows; each row contains the role | model | closing-line.
# Rich box-drawing splits on │ (U+2502).
extract_closing_lines() {
  local file="$1"
  strip_ansi <"${file}" \
    | awk -F'│' 'NF>=4 { gsub(/^ +| +$/, "", $2); gsub(/^ +| +$/, "", $4);
                         if ($2 != "" && $2 != "Voice") print $2 ": " $4 }' \
    | head -n5
}

# Header for the aggregated doc.
{
  echo "# Gecko self-research stress matrix"
  echo ""
  echo "**Date:** 2026-04-30"
  echo "**Mode:** ${MODE}"
  echo "**Source script:** \`scripts/positioning_check.sh\`"
  echo "**Raw transcripts:** \`docs/positioning/raw/2026-04-30/\`"
  echo ""
  echo "${TOTAL} idea variants run through the full \`bb research\` +"
  echo "\`bb plan\` (5-voice advisor panel) pipeline. Each row records the"
  echo "verdict (inferred from gap_classification per Sprint 9 S9-VERDICT-01),"
  echo "the structured gap label, and a count of unique sources cited."
  echo ""
  echo "## Summary"
  echo ""
  echo "| # | Idea | Verdict | Gap class | Sources | Notes |"
  echo "|---|------|---------|-----------|---------|-------|"
} > "${OUT_FILE}"

declare -a ROWS_DETAIL=()
declare -a IDEAS_FAILED=()

for i in "${!IDEAS[@]}"; do
  IDEA="${IDEAS[$i]}"
  N=$((i + 1))
  echo ""
  echo "[positioning] (${N}/${TOTAL}) ${IDEA}"

  RES_LOG="${RAW_DIR}/idea-${N}-research.log"
  PLAN_LOG="${RAW_DIR}/idea-${N}-plan.log"

  set +e
  BB --yes research --idea "${IDEA}" --tier basic >"${RES_LOG}" 2>&1
  RES_RC=$?
  set -e

  if [[ ${RES_RC} -ne 0 ]]; then
    echo "[positioning]   research failed (rc=${RES_RC}); see ${RES_LOG}"
    IDEAS_FAILED+=("${N}: research rc=${RES_RC}")
    printf "| %d | %s | ERROR | — | 0 | research failed (rc=%d) |\n" \
      "${N}" "${IDEA}" "${RES_RC}" >> "${OUT_FILE}"
    continue
  fi

  SID="$(extract_session_id "${RES_LOG}")"
  if [[ -z "${SID}" ]]; then
    echo "[positioning]   could not parse session_id from research output"
    IDEAS_FAILED+=("${N}: no session_id")
    printf "| %d | %s | ERROR | — | 0 | session_id unparsed |\n" \
      "${N}" "${IDEA}" >> "${OUT_FILE}"
    continue
  fi

  GAP_LINE="$(extract_gap "${RES_LOG}")"
  GAP_CLASS="$(echo "${GAP_LINE}" | awk -F' — ' '{print $1}')"
  GAP_SUMMARY="$(echo "${GAP_LINE}" | awk -F' — ' '{print $2}')"
  VERDICT="$(infer_verdict "${GAP_LINE}")"
  SRC_COUNT="$(count_sources "${RES_LOG}")"

  echo "[positioning]   session_id=${SID} verdict=${VERDICT} gap=${GAP_CLASS:-unknown}"

  set +e
  BB plan "${SID}" --tier-preset balanced >"${PLAN_LOG}" 2>&1
  PLAN_RC=$?
  set -e

  PLAN_NOTE=""
  if [[ ${PLAN_RC} -ne 0 ]]; then
    PLAN_NOTE="plan rc=${PLAN_RC} (F17?)"
    echo "[positioning]   plan failed (rc=${PLAN_RC}); continuing — see ${PLAN_LOG}"
    IDEAS_FAILED+=("${N}: plan rc=${PLAN_RC}")
  fi

  printf "| %d | %s | %s | %s | %s | %s |\n" \
    "${N}" "${IDEA}" "${VERDICT}" "${GAP_CLASS:-—}" "${SRC_COUNT:-0}" "${PLAN_NOTE:-—}" \
    >> "${OUT_FILE}"

  # Capture per-idea section into a buffer; appended after the summary table.
  {
    echo ""
    echo "---"
    echo ""
    echo "## ${N}. ${IDEA}"
    echo ""
    echo "- **session_id:** \`${SID}\`"
    echo "- **verdict:** ${VERDICT}"
    echo "- **gap_classification:** ${GAP_CLASS:-unknown}"
    echo "- **gap_summary:** ${GAP_SUMMARY:-—}"
    echo "- **sources_count:** ${SRC_COUNT:-0}"
    echo ""
    echo "### Top sources"
    echo ""
    TOP="$(top_sources "${RES_LOG}")"
    if [[ -n "${TOP}" ]]; then
      while IFS= read -r url; do
        echo "- ${url}"
      done <<<"${TOP}"
    else
      echo "- _(none extracted)_"
    fi
    echo ""
    echo "### Advisor panel — closing lines"
    echo ""
    if [[ ${PLAN_RC} -ne 0 ]]; then
      echo "_Plan invocation failed (rc=${PLAN_RC}); see \`raw/2026-04-30/idea-${N}-plan.log\`._"
    else
      LINES="$(extract_closing_lines "${PLAN_LOG}")"
      if [[ -n "${LINES}" ]]; then
        while IFS= read -r ln; do
          echo "- ${ln}"
        done <<<"${LINES}"
      else
        echo "_No closing lines parsed from advisor table._"
      fi
    fi
  } >> "${OUT_FILE}.detail.tmp"
done

# Append the per-idea detail sections.
if [[ -f "${OUT_FILE}.detail.tmp" ]]; then
  cat "${OUT_FILE}.detail.tmp" >> "${OUT_FILE}"
  rm -f "${OUT_FILE}.detail.tmp"
fi

# Failure roll-up.
{
  echo ""
  echo "---"
  echo ""
  echo "## Run notes"
  echo ""
  if [[ ${#IDEAS_FAILED[@]} -eq 0 ]]; then
    echo "_All 5 ideas completed cleanly._"
  else
    echo "Partial failures (matrix continued past these):"
    echo ""
    for f in "${IDEAS_FAILED[@]}"; do
      echo "- ${f}"
    done
  fi
} >> "${OUT_FILE}"

echo ""
echo "[positioning] wrote ${OUT_FILE}"
echo "[positioning] raw logs in ${RAW_DIR}"
