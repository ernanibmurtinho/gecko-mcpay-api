#!/usr/bin/env bash
# =============================================================
# Push gecko-api secrets/config from a local .env (or environment) into AWS
# SSM Parameter Store as SecureString. Values are never printed — only the
# parameter name and result status.
#
# Usage:
#   ./infra/push-ssm-params.sh [--region us-east-2] [--env-file .env]
#
# Switching networks (devnet ↔ mainnet) post-deploy:
#   aws ssm put-parameter --name /gecko-api/X402_NETWORK \
#     --value 'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp' \
#     --type SecureString --overwrite --region us-east-2
#   aws ecs update-service --cluster gecko-api --service gecko-api \
#     --force-new-deployment --region us-east-2
# =============================================================
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-2}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

while [[ $# -gt 0 ]]; do
  case $1 in
    --region)   REGION="$2";   shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: env file '$ENV_FILE' not found" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

SSM_PREFIX="/gecko-api"

# All gecko-api secrets/config in one place. SSM param name on the left,
# shell variable name on the right (so we can rename params without renaming
# our env vars). Every value goes in as SecureString — there's no harm in
# encrypting non-secret config too, and it keeps the deploy uniform.
declare -A PARAMS=(
  # Database / external APIs
  [SUPABASE_URL]="SUPABASE_URL"
  [SUPABASE_SERVICE_ROLE_KEY]="SUPABASE_SERVICE_ROLE_KEY"
  [TAVILY_API_KEY]="TAVILY_API_KEY"
  [OPENAI_API_KEY]="OPENAI_API_KEY"
  [DEEPGRAM_API_KEY]="DEEPGRAM_API_KEY"

  # x402 (devnet ↔ mainnet via SSM update + force-new-deployment)
  [X402_MODE]="X402_MODE"
  [X402_NETWORK]="X402_NETWORK"
  [X402_FACILITATOR_URL]="X402_FACILITATOR_URL"
  [GECKO_WALLET_ADDRESS]="GECKO_WALLET_ADDRESS"
  [GECKO_WALLET_ADDRESS_BASE]="GECKO_WALLET_ADDRESS_BASE"
  [RESEARCH_BASIC_PRICE]="RESEARCH_BASIC_PRICE"
  [RESEARCH_PRO_PRICE]="RESEARCH_PRO_PRICE"

  # LLM endpoint
  [GECKO_LLM_ENDPOINT]="GECKO_LLM_ENDPOINT"
  [GECKO_LLM_API_KEY]="GECKO_LLM_API_KEY"
  [CHAT_MODEL]="CHAT_MODEL"

  # Sprint 1 LLM routing (S1-01) — added 2026-04-28
  # LLM_ROUTER selects the OpenAI-compatible base URL: openai | openrouter | clawrouter
  # OPENROUTER_API_KEY is required only when LLM_ROUTER=openrouter; empty for openai.
  [LLM_ROUTER]="LLM_ROUTER"
  [OPENROUTER_API_KEY]="OPENROUTER_API_KEY"

  # Sprint 1 events token (S1-05) — added 2026-04-28
  # HMAC secret for Pro tier SSE events tokens + retry tokens.
  [EVENTS_SECRET]="EVENTS_SECRET"

  # Sprint 2 (S2-02/03) — CDP facilitator credentials. Required when
  # X402_NETWORK=solana-mainnet. On devnet they're ignored. Pushed even when
  # empty (sentinels) so the ECS task def `secrets:` ValueFrom resolves
  # without an init error before mainnet onboarding completes.
  [CDP_API_KEY_ID]="CDP_API_KEY_ID"
  [CDP_API_KEY_SECRET]="CDP_API_KEY_SECRET"

  # Sprint 2 (S2-05) — Privy v2 server-side wallet credentials. Required
  # only when per-project wallet provisioning is desired; sentinels keep the
  # task booting cleanly until onboarding completes.
  [PRIVY_APP_ID]="PRIVY_APP_ID"
  [PRIVY_APP_SECRET]="PRIVY_APP_SECRET"

  # Sprint 2 Final (S2X-09) — twit.sh x402 micropayments on Base mainnet.
  # Server-managed Gecko-owned EVM wallet. Disabled by default (kill-switch
  # via TWITSH_ENABLED=false). Sentinels keep ECS booting before the wallet
  # is funded; settings.is_twitsh_configured() gates real network calls.
  [TWITSH_WALLET_PRIVATE_KEY]="TWITSH_WALLET_PRIVATE_KEY"
  [TWITSH_WALLET_ADDRESS]="TWITSH_WALLET_ADDRESS"
  [TWITSH_ENABLED]="TWITSH_ENABLED"
  [TWITSH_BASE_URL]="TWITSH_BASE_URL"
)

echo "==> Region:     $REGION"
echo "==> SSM prefix: $SSM_PREFIX"
echo "==> Env file:   $ENV_FILE"
echo ""

# Params that ECS task CFN references as `secrets:` ValueFrom — these MUST
# exist in SSM even if empty, otherwise ResourceInitializationError on task
# start ("invalid ssm parameters: ..."). For these, push a sentinel value
# when the env var is empty; runtime code is expected to treat the sentinel
# as "unset" (it does — see gecko_core.orchestration.pro.router for
# OPENROUTER_API_KEY handling).
declare -A REQUIRED_AT_BOOT=(
  [LLM_ROUTER]="openai"
  [OPENROUTER_API_KEY]="__unset__"
  [EVENTS_SECRET]="__dev_change_me__"
  # CDP creds: sentinel keeps ECS task spinning up before onboarding. Code
  # treats `__unset__` as truly unset and refuses to boot mainnet without
  # real values — see gecko_core.payments.cdp.is_unconfigured.
  [CDP_API_KEY_ID]="__unset__"
  [CDP_API_KEY_SECRET]="__unset__"
  # Privy creds — same sentinel pattern as CDP. gecko_core.wallets.privy
  # treats `__unset__` as truly unset and lazy-skips wallet provisioning.
  [PRIVY_APP_ID]="__unset__"
  [PRIVY_APP_SECRET]="__unset__"
  # twit.sh — kill-switch defaults to false; sentinels on wallet creds keep
  # the integration silently disabled regardless of TWITSH_ENABLED.
  [TWITSH_WALLET_PRIVATE_KEY]="__unset__"
  [TWITSH_WALLET_ADDRESS]="__unset__"
  [TWITSH_ENABLED]="false"
  [TWITSH_BASE_URL]="https://x402.twit.sh"
)

SKIPPED=()
PUSHED=()
PLACEHOLDED=()

for PARAM_NAME in "${!PARAMS[@]}"; do
  VAR_NAME="${PARAMS[$PARAM_NAME]}"
  VALUE="${!VAR_NAME:-}"

  if [[ -z "$VALUE" ]]; then
    if [[ -n "${REQUIRED_AT_BOOT[$PARAM_NAME]:-}" ]]; then
      VALUE="${REQUIRED_AT_BOOT[$PARAM_NAME]}"
      echo "  PLACEHOLDER  $SSM_PREFIX/$PARAM_NAME  (${VAR_NAME} empty; pushing sentinel '$VALUE')"
      PLACEHOLDED+=("$PARAM_NAME")
    else
      echo "  SKIP  $SSM_PREFIX/$PARAM_NAME  (${VAR_NAME} is empty in $ENV_FILE)"
      SKIPPED+=("$PARAM_NAME")
      continue
    fi
  fi

  aws ssm put-parameter \
    --name "${SSM_PREFIX}/${PARAM_NAME}" \
    --value "$VALUE" \
    --type SecureString \
    --overwrite \
    --region "$REGION" \
    --output text \
    --query 'Version' \
    | xargs -I{} echo "  OK    $SSM_PREFIX/$PARAM_NAME  (version {})"

  PUSHED+=("$PARAM_NAME")
done

echo ""
echo "==> Done. ${#PUSHED[@]} pushed, ${#PLACEHOLDED[@]} placeholders, ${#SKIPPED[@]} skipped."
if [[ ${#PLACEHOLDED[@]} -gt 0 ]]; then
  echo "    Placeholder sentinels (set real values via .env or aws ssm put-parameter):"
  for P in "${PLACEHOLDED[@]}"; do echo "      - $SSM_PREFIX/$P"; done
fi

if [[ ${#SKIPPED[@]} -gt 0 ]]; then
  echo ""
  echo "Skipped (fill in $ENV_FILE and re-run):"
  for P in "${SKIPPED[@]}"; do
    echo "  - $SSM_PREFIX/$P"
  done
fi

echo ""
echo "Quick reference for two big knobs:"
echo "  X402_NETWORK=solana-devnet                              # devnet (default)"
echo "  X402_NETWORK=solana-mainnet                             # mainnet-beta (CDP)"
echo "  # Legacy CAIP-2 form still accepted with a deprecation warning:"
echo "  # X402_NETWORK=solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1  # devnet"
echo "  # X402_NETWORK=solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp  # mainnet-beta"
echo "  RESEARCH_BASIC_PRICE='\$0.10'                           # devnet test"
echo "  RESEARCH_BASIC_PRICE='\$0.50'                           # mainnet starter"
echo "  RESEARCH_BASIC_PRICE='\$20.00'                          # production target"
