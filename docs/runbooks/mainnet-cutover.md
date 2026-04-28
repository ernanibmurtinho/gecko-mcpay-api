# Mainnet Cutover Runbook

**Status:** Live operator runbook. Sprint 2. Devnet → Mainnet via CDP facilitator.
**Audience:** On-call operator (Ernani). Read top to bottom. Do not skip verifications.
**Region:** AWS `us-east-2`. **Cluster:** `gecko-api`. **Service (primary):** `gecko-api`.

Set these once at the top of your shell session:

```bash
export AWS_REGION=us-east-2
export CLUSTER=gecko-api
export SERVICE=gecko-api
export SERVICE_CANARY=gecko-api-mainnet
```

---

## 1. Pre-cutover checklist

Every box must be ticked before Section 2.

### 1.1 Generate the mainnet keypair

- Generate offline (air-gapped laptop preferred):

```bash
solana-keygen new --no-bip39-passphrase --outfile ~/gecko-mainnet-recipient.json
solana-keygen pubkey ~/gecko-mainnet-recipient.json
```

- Verify with:

```bash
test -s ~/gecko-mainnet-recipient.json && echo "key file present"
solana-keygen verify $(solana-keygen pubkey ~/gecko-mainnet-recipient.json) ~/gecko-mainnet-recipient.json
```

- Confirm pubkey is **NOT** `jhR4np114TdWcAd6R69kArk6DWPviBp7B8BwCw9Xkux` (devnet key — never reuse).

### 1.2 Push keypair to SSM

```bash
aws ssm put-parameter --name /gecko-api/MAINNET_RECIPIENT_PRIVATE_KEY \
  --value "$(cat ~/gecko-mainnet-recipient.json)" --type SecureString --overwrite --region $AWS_REGION

aws ssm put-parameter --name /gecko-api/MAINNET_RECIPIENT_ADDRESS \
  --value "$(solana-keygen pubkey ~/gecko-mainnet-recipient.json)" --type SecureString --overwrite --region $AWS_REGION
```

- Verify with:

```bash
aws ssm get-parameter --name /gecko-api/MAINNET_RECIPIENT_ADDRESS --with-decryption --region $AWS_REGION --query Parameter.Value --output text
aws ssm get-parameter --name /gecko-api/MAINNET_RECIPIENT_PRIVATE_KEY --with-decryption --region $AWS_REGION --query Parameter.Value --output text | jq 'length'
```

- Shred local copy after SSM confirms:

```bash
shred -u ~/gecko-mainnet-recipient.json
```

### 1.3 Fund the recipient with $20 USDC mainnet

- USDC mainnet mint: `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`
- Send $20 USDC from your funded wallet to the address from step 1.2.
- Verify with:

```bash
ADDR=$(aws ssm get-parameter --name /gecko-api/MAINNET_RECIPIENT_ADDRESS --with-decryption --region $AWS_REGION --query Parameter.Value --output text)
spl-token balance EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v --owner $ADDR --url https://api.mainnet-beta.solana.com
```

- Expect: `20` (or close — account for transfer fees).
- Cross-check on Solscan: `https://solscan.io/account/$ADDR`.

### 1.4 CDP credentials in SSM

- Source: Coinbase Cloud → Developer Platform → API Keys. Create key scoped to x402.

```bash
aws ssm put-parameter --name /gecko-api/CDP_API_KEY_ID --value "<key-id>" --type SecureString --overwrite --region $AWS_REGION
aws ssm put-parameter --name /gecko-api/CDP_API_KEY_SECRET --value "<key-secret>" --type SecureString --overwrite --region $AWS_REGION
aws ssm put-parameter --name /gecko-api/CDP_FACILITATOR_URL --value "https://api.cdp.coinbase.com/platform/v2/x402" --type String --overwrite --region $AWS_REGION
```

- Verify with:

```bash
for k in CDP_API_KEY_ID CDP_API_KEY_SECRET CDP_FACILITATOR_URL; do
  aws ssm get-parameter --name /gecko-api/$k --with-decryption --region $AWS_REGION --query Parameter.Value --output text | head -c 8; echo " ...$k"
done
```

### 1.5 Apply Supabase migrations 012 + 013

- **Manual step** — open Supabase SQL editor and paste:
  - `infra/supabase/migrations/012_x402_settlements_network.sql`
  - `infra/supabase/migrations/013_project_wallets.sql`
- Verify with (psql against the prod connection string):

```bash
psql "$SUPABASE_DB_URL" -c "select column_name from information_schema.columns where table_name='sessions' and column_name='network';"
psql "$SUPABASE_DB_URL" -c "select column_name from information_schema.columns where table_name='projects' and column_name='privy_wallet_id';"
```

- Both must return without error and show the new columns.

### 1.6 Privy app credentials

- Source: Privy Dashboard → app `gecko-prod` → Settings.

```bash
aws ssm put-parameter --name /gecko-api/PRIVY_APP_ID --value "<app-id>" --type SecureString --overwrite --region $AWS_REGION
aws ssm put-parameter --name /gecko-api/PRIVY_APP_SECRET --value "<app-secret>" --type SecureString --overwrite --region $AWS_REGION
```

- Verify with:

```bash
aws ssm get-parameter --name /gecko-api/PRIVY_APP_ID --with-decryption --region $AWS_REGION --query Parameter.Value --output text
```

### 1.7 Eval baseline exists

```bash
test -f tests/eval/baselines/2026-04-28-4.json && jq '.aggregate.verdict_accuracy' tests/eval/baselines/2026-04-28-4.json
```

- Expect `0.85`.

### 1.8 Latest image deployed on devnet

- Confirm S2-02/S2-03 code is live on prod still pinned to devnet:

```bash
curl -sS https://api.geckovision.tech/openapi.json | jq '.paths."/research".post.responses."402"' | head -40
aws ecs describe-services --cluster $CLUSTER --services $SERVICE --region $AWS_REGION \
  --query 'services[0].taskDefinition' --output text
```

- The 402 challenge block must be present. Note the task definition ARN — that is your rollback target.

### 1.9 Tag image in ECR for rollback

```bash
TASKDEF=$(aws ecs describe-services --cluster $CLUSTER --services $SERVICE --region $AWS_REGION --query 'services[0].taskDefinition' --output text)
IMAGE=$(aws ecs describe-task-definition --task-definition $TASKDEF --region $AWS_REGION --query 'taskDefinition.containerDefinitions[0].image' --output text)
echo "ROLLBACK IMAGE: $IMAGE"

# Re-tag in ECR
REPO=$(echo $IMAGE | cut -d: -f1 | cut -d/ -f2-)
DIGEST=$(aws ecr describe-images --repository-name $REPO --image-ids imageTag=$(echo $IMAGE | cut -d: -f2) --region $AWS_REGION --query 'imageDetails[0].imageDigest' --output text)
MANIFEST=$(aws ecr batch-get-image --repository-name $REPO --image-ids imageDigest=$DIGEST --region $AWS_REGION --query 'images[0].imageManifest' --output text)
aws ecr put-image --repository-name $REPO --image-tag pre-mainnet-rollback --image-manifest "$MANIFEST" --region $AWS_REGION
```

- Verify with:

```bash
aws ecr describe-images --repository-name $REPO --image-ids imageTag=pre-mainnet-rollback --region $AWS_REGION --query 'imageDetails[0].imageTags'
```

---

## 2. Stand up the mainnet canary service

ALB cannot split traffic by request body. Solution: a second ECS service (`gecko-api-mainnet`), same task definition family, env var override `X402_NETWORK=solana-mainnet`. Both services attach to separate target groups behind one ALB listener rule with weighted forwarding (10% mainnet / 90% devnet).

### 2.1 Create the mainnet target group

```bash
VPC_ID=$(aws elbv2 describe-target-groups --names gecko-api-tg --region $AWS_REGION --query 'TargetGroups[0].VpcId' --output text)

aws elbv2 create-target-group --name gecko-api-mainnet-tg \
  --protocol HTTP --port 8000 --vpc-id $VPC_ID --target-type ip \
  --health-check-path /healthz --health-check-interval-seconds 15 \
  --region $AWS_REGION
```

- Verify with:

```bash
aws elbv2 describe-target-groups --names gecko-api-mainnet-tg --region $AWS_REGION --query 'TargetGroups[0].TargetGroupArn'
```

### 2.2 Register a new task definition revision pinned to mainnet

- Copy current task def, set `X402_NETWORK=solana-mainnet`, register:

```bash
aws ecs describe-task-definition --task-definition $TASKDEF --region $AWS_REGION \
  --query 'taskDefinition' > /tmp/td.json

jq '.containerDefinitions[0].environment |= (map(select(.name!="X402_NETWORK")) + [{"name":"X402_NETWORK","value":"solana-mainnet"}]) | {family, taskRoleArn, executionRoleArn, networkMode, containerDefinitions, requiresCompatibilities, cpu, memory}' /tmp/td.json > /tmp/td-mainnet.json

aws ecs register-task-definition --cli-input-json file:///tmp/td-mainnet.json --region $AWS_REGION \
  --family gecko-api-mainnet --query 'taskDefinition.taskDefinitionArn' --output text
```

- Verify with:

```bash
aws ecs describe-task-definition --task-definition gecko-api-mainnet --region $AWS_REGION \
  --query 'taskDefinition.containerDefinitions[0].environment[?name==`X402_NETWORK`].value' --output text
```

- Expect `solana-mainnet`.

### 2.3 Create the mainnet service

```bash
TG_MAINNET=$(aws elbv2 describe-target-groups --names gecko-api-mainnet-tg --region $AWS_REGION --query 'TargetGroups[0].TargetGroupArn' --output text)
SUBNETS=$(aws ecs describe-services --cluster $CLUSTER --services $SERVICE --region $AWS_REGION --query 'services[0].networkConfiguration.awsvpcConfiguration.subnets' --output json)
SGS=$(aws ecs describe-services --cluster $CLUSTER --services $SERVICE --region $AWS_REGION --query 'services[0].networkConfiguration.awsvpcConfiguration.securityGroups' --output json)

aws ecs create-service --cluster $CLUSTER --service-name $SERVICE_CANARY \
  --task-definition gecko-api-mainnet --desired-count 1 --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=$SUBNETS,securityGroups=$SGS,assignPublicIp=DISABLED}" \
  --load-balancers "targetGroupArn=$TG_MAINNET,containerName=gecko-api,containerPort=8000" \
  --region $AWS_REGION
```

- Verify with:

```bash
aws ecs describe-services --cluster $CLUSTER --services $SERVICE_CANARY --region $AWS_REGION \
  --query 'services[0].{running:runningCount,desired:desiredCount,status:status}'
aws elbv2 describe-target-health --target-group-arn $TG_MAINNET --region $AWS_REGION \
  --query 'TargetHealthDescriptions[].TargetHealth.State'
```

- All targets must be `healthy` before step 2.4. Wait until they are.

### 2.4 Flip the listener rule to weighted 10% mainnet / 90% devnet

```bash
LISTENER_ARN=$(aws elbv2 describe-listeners --load-balancer-arn $(aws elbv2 describe-load-balancers --names gecko-api-alb --region $AWS_REGION --query 'LoadBalancers[0].LoadBalancerArn' --output text) --region $AWS_REGION --query 'Listeners[?Port==`443`].ListenerArn' --output text)
TG_DEVNET=$(aws elbv2 describe-target-groups --names gecko-api-tg --region $AWS_REGION --query 'TargetGroups[0].TargetGroupArn' --output text)

aws elbv2 modify-listener --listener-arn $LISTENER_ARN --region $AWS_REGION \
  --default-actions "Type=forward,ForwardConfig={TargetGroups=[{TargetGroupArn=$TG_DEVNET,Weight=90},{TargetGroupArn=$TG_MAINNET,Weight=10}]}"
```

- Verify with:

```bash
aws elbv2 describe-listeners --listener-arns $LISTENER_ARN --region $AWS_REGION \
  --query 'Listeners[0].DefaultActions[0].ForwardConfig.TargetGroups'
```

- Note start time. The 24h canary window begins now.

### 2.5 Alarms to watch (CloudWatch)

- `gecko-api-mainnet 5xx rate` — alarm > 1% over 5min
- `CDP facilitator 4xx rate` (custom metric `cdp.client.4xx`) — alarm > 2%
- `x402.settlement.failed` (custom counter) — alarm any non-zero over 5min
- `ALB TargetResponseTime p99 mainnet TG` — alarm > 2x devnet p99
- Live tail:

```bash
aws logs tail /ecs/gecko-api-mainnet --follow --region $AWS_REGION --filter-pattern "ERROR x402 settlement"
```

### 2.6 Stop conditions (abort canary immediately)

- > 5% settlement failure rate over any rolling 15min window
- Any signed-but-unconfirmed mainnet tx older than 90s (potential tx loss)
- Any 5xx burst on the CDP path (>3 in 60s)
- If hit: jump to **Section 6. Emergency stop**.

### 2.7 Roll-forward criteria after 24h

All must hold:

- Zero failed mainnet settlements: `select count(*) from sessions where network='solana-mainnet' and x402_tx_signature is null and updated_at > now() - interval '24 hours';` must equal `0`.
- Mainnet p99 latency < 2× devnet p99.
- At least 5 successful mainnet txs in `sessions` where `network='solana-mainnet'`.

```bash
psql "$SUPABASE_DB_URL" -c "select network, count(*) from sessions where created_at > now() - interval '24 hours' group by network;"
```

---

## 3. Full cutover to 100% mainnet

### 3.1 Flip the SSM network parameter

```bash
aws ssm put-parameter --name /gecko-api/X402_NETWORK --value solana-mainnet --type SecureString --overwrite --region $AWS_REGION
```

- Verify with:

```bash
aws ssm get-parameter --name /gecko-api/X402_NETWORK --with-decryption --region $AWS_REGION --query Parameter.Value --output text
```

### 3.2 Force-new-deployment on the primary service

```bash
aws ecs update-service --cluster $CLUSTER --service $SERVICE --force-new-deployment --region $AWS_REGION
```

- Verify with:

```bash
aws ecs wait services-stable --cluster $CLUSTER --services $SERVICE --region $AWS_REGION
aws ecs describe-services --cluster $CLUSTER --services $SERVICE --region $AWS_REGION \
  --query 'services[0].deployments[0].{status:status,running:runningCount,desired:desiredCount}'
```

### 3.3 Restore listener to 100% primary target group

```bash
aws elbv2 modify-listener --listener-arn $LISTENER_ARN --region $AWS_REGION \
  --default-actions "Type=forward,TargetGroupArn=$TG_DEVNET"
```

(The TG name still says `devnet` for historical reasons. The service behind it is now mainnet. Rename in a follow-up; do not rename mid-cutover.)

- Verify with:

```bash
aws elbv2 describe-listeners --listener-arns $LISTENER_ARN --region $AWS_REGION --query 'Listeners[0].DefaultActions'
```

### 3.4 Keep canary service alive for 48h rollback window

- Do **not** delete `gecko-api-mainnet` service yet. Scale to 0:

```bash
aws ecs update-service --cluster $CLUSTER --service $SERVICE_CANARY --desired-count 0 --region $AWS_REGION
```

- Schedule deletion 48h after step 3.2 completes.

### 3.5 Update marketing copy (gecko-mcpay-landing)

- Search for `devnet` in landing repo; replace with `Solana mainnet`. PR + merge.
- Verify with:

```bash
curl -sS https://geckovision.tech | grep -i devnet || echo "clean"
```

### 3.6 Update gecko-mcpay-skills `skill.md`

- Search the skills repo for any `devnet` mention; replace.
- Verify with:

```bash
curl -sS https://app.geckovision.tech/skill.md | grep -i devnet || echo "clean"
```

---

## 4. Rollback to devnet

Time budget: ~5 minutes.

### 4.1 Flip SSM back

```bash
aws ssm put-parameter --name /gecko-api/X402_NETWORK --value solana-devnet --type SecureString --overwrite --region $AWS_REGION
```

### 4.2 Force-new-deployment

```bash
aws ecs update-service --cluster $CLUSTER --service $SERVICE --force-new-deployment --region $AWS_REGION
aws ecs wait services-stable --cluster $CLUSTER --services $SERVICE --region $AWS_REGION
```

- Verify with:

```bash
curl -sS https://api.geckovision.tech/healthz
curl -sS -X POST https://api.geckovision.tech/research -H 'content-type: application/json' -d '{"idea":"x"}' -o /dev/null -w '%{http_code}\n'
```

- Expect `402` with devnet chain_id in body.

### 4.3 In-flight session behavior (spec — operator awareness)

- Sessions that paid on mainnet during the cutover **complete on mainnet**. Source of truth: `sessions.network` (migration 012). The retry token from V11-02 is session-scoped, so it carries the original network.
- New `/research` calls after step 4.2 are devnet.
- No manual reconciliation required. Verify with:

```bash
psql "$SUPABASE_DB_URL" -c "select network, count(*) from sessions where created_at > now() - interval '1 hour' group by network;"
```

- Both networks may appear during the cutover — that is expected.

### 4.4 Worst-case: roll back to the pre-mainnet image

```bash
# Use the ECR tag pre-mainnet-rollback created in step 1.9
aws ecs update-service --cluster $CLUSTER --service $SERVICE --task-definition <previous-family> --force-new-deployment --region $AWS_REGION
```

---

## 5. Post-cutover verification

### 5.1 Check OpenAPI 402 challenge

```bash
curl -sS https://api.geckovision.tech/openapi.json | jq '.. | objects | select(.chain_id?) | .chain_id' | sort -u
```

- Expect: a Solana mainnet CAIP-2 (`solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`). Must **not** include the devnet `EtWTRABZaYq6iMfeYKouRu166VU2xqa1`.

### 5.2 First real-money paid call from Claude Code

- In Claude Code:

```
Use gecko_research --tier basic with idea: "Solana validator economics 2026"
```

- Note the `x402_tx_signature` from the response.
- Verify with:

```bash
SIG=<paste-signature>
open "https://solscan.io/tx/$SIG"
```

- URL must resolve to a **mainnet** tx (no `?cluster=devnet`).

### 5.3 Eval regression

```bash
uv run python -m tests.eval.runner --baseline tests/eval/baselines/2026-04-28-4.json
```

- Mock cannot test mainnet settlement. This step confirms prompts v4 still produce `verdict_accuracy >= 0.85`.

### 5.4 sessions.network column

```bash
psql "$SUPABASE_DB_URL" -c "select s.network, count(*), sum(c.cost_usd) from sessions s left join session_costs c on c.session_id=s.id where s.created_at > now() - interval '1 hour' group by s.network;"
```

- New rows must show `solana-mainnet`.

### 5.5 Wallet balance

```bash
ADDR=$(aws ssm get-parameter --name /gecko-api/MAINNET_RECIPIENT_ADDRESS --with-decryption --region $AWS_REGION --query Parameter.Value --output text)
echo "https://solscan.io/account/$ADDR"
spl-token balance EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v --owner $ADDR --url https://api.mainnet-beta.solana.com
```

- Balance should equal `20 + (0.10 × N successful basic-tier sessions) + (0.75 × M successful pro-tier sessions)` minus CDP fees.

---

## 6. Emergency stop

If on fire, run this single block:

```bash
aws ssm put-parameter --name /gecko-api/X402_NETWORK --value solana-devnet --type SecureString --overwrite --region $AWS_REGION \
  && aws ecs update-service --cluster $CLUSTER --service $SERVICE --force-new-deployment --region $AWS_REGION \
  && aws elbv2 modify-listener --listener-arn $LISTENER_ARN --region $AWS_REGION \
       --default-actions "Type=forward,TargetGroupArn=$TG_DEVNET" \
  && aws ecs update-service --cluster $CLUSTER --service $SERVICE_CANARY --desired-count 0 --region $AWS_REGION
```

- ~5 minutes from execution to no mainnet traffic.
- Verify with:

```bash
aws ecs wait services-stable --cluster $CLUSTER --services $SERVICE --region $AWS_REGION
curl -sS https://api.geckovision.tech/openapi.json | jq '.. | objects | select(.chain_id?) | .chain_id' | sort -u
```

### Communication

- **Primary on-call:** Ernani (ernanibmurtinho@gmail.com)
- **Public status:** X handle from `gecko-mcpay-landing` site footer (`x.com/ernanibritto`)
- **Inbound user reports:** frames.ag DM
- **Incident log:** open a GitHub issue in `gecko-mcpay-api` titled `incident: mainnet cutover YYYY-MM-DD` and link CloudWatch screenshots + tx signatures

---

## 7. Open questions (cannot be pre-answered)

- **CDP signing edge cases** — first-real-call validation. The CDP facilitator's exact error shape for insufficient-fee, signature-replay, and slot-skipped cases is not documented to our satisfaction. Watch step 5.2 closely; capture full response body in CloudWatch.
- **Privy rate limits during burst project creation** — the lazy-create-on-first-paid-call flow may hit Privy QPS ceilings if a launch tweet triggers a burst. No public limit doc; first-real-call validation. If it bites, queue project wallet creation behind a small async worker (out of scope for this runbook).
- **Solana mainnet RPC congestion** — historically chronic. Recommend executing cutover during off-peak hours (US night). If RPC fails during the canary, Section 6 Emergency Stop. Do not attempt to swap RPC providers mid-cutover.
- **Pricing review at mainnet COGS + CDP fees** — defer to `business-manager`. $0.75 pro-tier and $0.10 basic-tier were set against devnet (zero settlement cost). Real CDP fee plus real Solana priority fee may push margin below target. **Action: business-manager should run the math after the first 100 mainnet sessions.**
