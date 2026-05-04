#!/usr/bin/env bash
# =============================================================
# gecko-api — ECS deploy script
#
# Usage:
#   ./infra/deploy.sh [--region us-east-2] [--env production] [--cert ARN] [--skip-build]
#
# Prerequisites:
#   - AWS CLI configured (aws configure or IAM role)
#   - Docker running
#   - SSM parameters created — run ./infra/push-ssm-params.sh first
#   - ACM certificate for api.geckovision.tech in the target region (for HTTPS)
#
# Adapted from ../gecko-social-fi-creators-api/infra/deploy.sh.
# =============================================================
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-2}"
STACK_NAME="gecko-api-ecs"
ENVIRONMENT="production"
SSM_PREFIX="/gecko-api"
ECR_REPOSITORY="gecko-api"
CERTIFICATE_ARN=""
SKIP_BUILD=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --region)     REGION="$2";          shift 2 ;;
    --env)        ENVIRONMENT="$2";     shift 2 ;;
    --stack)      STACK_NAME="$2";      shift 2 ;;
    --cert)       CERTIFICATE_ARN="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=true;      shift ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

echo "==> Region:      $REGION"
echo "==> Stack:       $STACK_NAME"
echo "==> Environment: $ENVIRONMENT"
echo "==> ECR repo:    $ECR_REPOSITORY"
echo "==> Skip build:  $SKIP_BUILD"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPOSITORY}"
# Include a timestamp so the tag always changes, forcing CloudFormation to
# update the task definition even when the git SHA hasn't changed (e.g. a
# redeploy to pick up env var or infra changes without new commits).
IMAGE_TAG="${ENVIRONMENT}-$(git rev-parse --short HEAD 2>/dev/null || echo latest)-$(date +%s)"
FULL_IMAGE="${ECR_URI}:${IMAGE_TAG}"
# CF_IMAGE is what CloudFormation uses in the task definition. Using the
# versioned (timestamped) tag means every deploy updates the task definition
# and ECS picks up the fresh image — no stale-layer surprises.
CF_IMAGE="$FULL_IMAGE"

echo "==> ECR image:   $FULL_IMAGE"
echo "==> CF image:    $CF_IMAGE"

if [[ "$SKIP_BUILD" == false ]]; then
  echo "==> Logging into ECR..."
  aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

  echo "==> Ensuring ECR repository exists..."
  aws ecr create-repository \
    --repository-name "$ECR_REPOSITORY" \
    --image-scanning-configuration scanOnPush=true \
    --region "$REGION" 2>/dev/null || true

  # Building for linux/amd64 explicitly: macOS dev machines default to arm64,
  # but ECS Fargate runs amd64 unless you opt in to Graviton — and our task
  # def doesn't, so amd64 it is.
  echo "==> Building Docker image (linux/amd64)..."
  docker buildx build --platform linux/amd64 -t gecko-api --load "$REPO_ROOT"

  docker tag gecko-api "$FULL_IMAGE"
  docker tag gecko-api "${ECR_URI}:${ENVIRONMENT}-latest"

  echo "==> Pushing image to ECR..."
  docker push "$FULL_IMAGE"
  docker push "${ECR_URI}:${ENVIRONMENT}-latest"
  echo "==> Image pushed: $FULL_IMAGE"
else
  echo "==> Skipping Docker build/push."
fi

STACK_STATUS=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].StackStatus' \
  --output text 2>/dev/null || echo "DOES_NOT_EXIST")

if [[ "$STACK_STATUS" == "REVIEW_IN_PROGRESS" || "$STACK_STATUS" == "ROLLBACK_COMPLETE" ]]; then
  echo "==> Stack is in '$STACK_STATUS' — deleting before redeploy..."
  aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
  echo "==> Stack deleted."
fi

echo "==> Deploying CloudFormation stack '$STACK_NAME'..."
aws cloudformation deploy \
  --template-file "$SCRIPT_DIR/ecs-stack.yml" \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    Image="$CF_IMAGE" \
    SSMPrefix="$SSM_PREFIX" \
    Environment="$ENVIRONMENT" \
    CertificateArn="$CERTIFICATE_ARN" \
  --no-fail-on-empty-changeset

# When --skip-build is used, we still need to nudge ECS to pull the latest
# image even though the CloudFormation tag hasn't changed.
if [[ "$SKIP_BUILD" == false ]]; then
  echo "==> Forcing ECS service to pick up the new image..."
  aws ecs update-service \
    --cluster gecko-api \
    --service gecko-api \
    --force-new-deployment \
    --region "$REGION" \
    --output text \
    --query 'service.deployments[0].{status:rolloutState,desired:desiredCount}' \
    >/dev/null || true
fi

echo ""
echo "==> Stack outputs:"
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table

ALB_DNS=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`ALBDNSName`].OutputValue' \
  --output text)

echo ""
echo "==> Done!"
echo "    ALB DNS  : $ALB_DNS"
echo "    A-alias  : api.geckovision.tech → $ALB_DNS"
echo "    Health   : curl https://api.geckovision.tech/healthz   (after DNS + cert)"
echo ""
echo "Next steps:"
echo "  1. Route 53 → A-record (alias) api.geckovision.tech → $ALB_DNS"
echo "  2. Without cert: curl http://$ALB_DNS/healthz"
echo "  3. Tail logs:   aws logs tail /ecs/gecko-api --follow --region $REGION"
echo "  4. Toggle network/price via SSM:"
echo "       aws ssm put-parameter --name $SSM_PREFIX/X402_NETWORK --value 'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp' --type SecureString --overwrite --region $REGION"
echo "       aws ecs update-service --cluster gecko-api --service gecko-api --force-new-deployment --region $REGION"
