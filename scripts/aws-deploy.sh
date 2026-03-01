#!/bin/bash
# Deploy, update, teardown, or check status of claude-agent on AWS EC2.
#
# Usage:
#   bash scripts/aws-deploy.sh create   --repo owner/repo [--region us-east-1] [--instance-type t3.small] [--image ghcr.io/owner/claude-agent:latest]
#   bash scripts/aws-deploy.sh update   --repo owner/repo [--region us-east-1]
#   bash scripts/aws-deploy.sh teardown --repo owner/repo [--region us-east-1]
#   bash scripts/aws-deploy.sh status   [--repo owner/repo] [--region us-east-1]
#
# All AWS resources are tagged with Project=claude-agent and TargetRepo=owner/repo.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="claude-agent"
DEFAULT_REGION="us-east-1"
DEFAULT_INSTANCE_TYPE="t3.small"
DEFAULT_IMAGE=""  # set from --repo if not specified
IAM_PROFILE="claude-agent-ssm-reader"

# ─── Argument Parsing ───

COMMAND="${1:-help}"
shift || true

REPO=""
REGION="$DEFAULT_REGION"
INSTANCE_TYPE="$DEFAULT_INSTANCE_TYPE"
AGENT_IMAGE=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --repo)           REPO="$2"; shift 2 ;;
    --region)         REGION="$2"; shift 2 ;;
    --instance-type)  INSTANCE_TYPE="$2"; shift 2 ;;
    --image)          AGENT_IMAGE="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

export AWS_DEFAULT_REGION="$REGION"

# ─── Helpers ───

repo_slug() {
  echo "$1" | tr '/' '-' | tr '[:upper:]' '[:lower:]'
}

require_repo() {
  if [[ -z "$REPO" ]]; then
    echo "Error: --repo owner/repo is required" >&2
    exit 1
  fi
}

verify_aws_identity() {
  echo "==> Verifying AWS identity"
  local identity
  identity=$(aws sts get-caller-identity --output json 2>/dev/null) || {
    echo "Error: AWS CLI is not configured or credentials are invalid." >&2
    echo "Run 'aws configure' or set AWS_PROFILE first." >&2
    exit 1
  }
  local account arn
  account=$(echo "$identity" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
  arn=$(echo "$identity" | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])")
  echo "    Account: $account"
  echo "    ARN:     $arn"
  echo ""
  read -r -p "Is this the correct AWS account? [y/N] " confirm
  if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Aborted. Switch AWS credentials and try again." >&2
    exit 1
  fi
}

find_instance() {
  local slug=$1
  aws ec2 describe-instances \
    --filters \
      "Name=tag:Project,Values=$PROJECT" \
      "Name=tag:TargetRepo,Values=$REPO" \
      "Name=instance-state-name,Values=running,pending,stopped" \
    --query 'Reservations[0].Instances[0].InstanceId' \
    --output text 2>/dev/null | grep -v '^None$' || true
}

find_security_group() {
  local slug=$1
  aws ec2 describe-security-groups \
    --filters \
      "Name=tag:Project,Values=$PROJECT" \
      "Name=tag:TargetRepo,Values=$REPO" \
    --query 'SecurityGroups[0].GroupId' \
    --output text 2>/dev/null | grep -v '^None$' || true
}

find_elastic_ip() {
  aws ec2 describe-addresses \
    --filters \
      "Name=tag:Project,Values=$PROJECT" \
      "Name=tag:TargetRepo,Values=$REPO" \
    --query 'Addresses[0]' \
    --output json 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data:
    print(json.dumps(data))
" 2>/dev/null || true
}

get_elastic_ip_address() {
  local eip_json=$1
  echo "$eip_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('PublicIp',''))" 2>/dev/null || true
}

get_elastic_ip_alloc_id() {
  local eip_json=$1
  echo "$eip_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('AllocationId',''))" 2>/dev/null || true
}

resolve_ami() {
  aws ec2 describe-images \
    --owners amazon \
    --filters \
      "Name=name,Values=al2023-ami-2023*-x86_64" \
      "Name=state,Values=available" \
    --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
    --output text
}

ssh_key_path() {
  echo "$HOME/.ssh/claude-agent-$(repo_slug "$REPO").pem"
}

tags_json() {
  local slug=$(repo_slug "$REPO")
  cat <<EOF
[
  {"Key":"Name","Value":"${PROJECT}-${slug}"},
  {"Key":"Project","Value":"${PROJECT}"},
  {"Key":"TargetRepo","Value":"${REPO}"},
  {"Key":"ManagedBy","Value":"claude-agent-deploy"}
]
EOF
}

tags_spec() {
  local resource_type=$1
  echo "ResourceType=${resource_type},Tags=$(tags_json)"
}

wait_for_health() {
  local ip=$1
  local url="http://${ip}:5000/health"
  echo "==> Waiting for health check at $url"
  local attempts=0
  local max_attempts=60
  while [[ $attempts -lt $max_attempts ]]; do
    if curl -sf "$url" >/dev/null 2>&1; then
      echo "    Health check passed!"
      return 0
    fi
    attempts=$((attempts + 1))
    echo "    Attempt $attempts/$max_attempts — waiting..."
    sleep 10
  done
  echo "    Warning: health check did not pass after ${max_attempts} attempts."
  echo "    The instance may still be initializing. Check logs via SSH."
  return 1
}

# ─── Commands ───

cmd_create() {
  require_repo
  verify_aws_identity

  local slug=$(repo_slug "$REPO")
  local key_file=$(ssh_key_path)

  if [[ -z "$AGENT_IMAGE" ]]; then
    AGENT_IMAGE="ghcr.io/${REPO}:latest"
  fi

  # Check for existing deployment
  local existing_instance=$(find_instance "$slug")
  if [[ -n "$existing_instance" ]]; then
    echo "Error: An instance already exists for $REPO (${existing_instance})." >&2
    echo "Run 'teardown' first, or 'update' to refresh." >&2
    exit 1
  fi

  echo ""
  echo "==> Deploying claude-agent for: $REPO"
  echo "    Region:    $REGION"
  echo "    Instance:  $INSTANCE_TYPE"
  echo "    Image:     $AGENT_IMAGE"
  echo ""

  # --- Collect and store secrets ---
  echo "==> Collecting secrets (input is hidden)"
  echo ""

  read -r -s -p "GITHUB_TOKEN (personal access token with repo scope): " GITHUB_TOKEN
  echo ""
  read -r -s -p "GITHUB_WEBHOOK_SECRET (or press Enter to auto-generate): " GITHUB_WEBHOOK_SECRET
  echo ""
  if [[ -z "$GITHUB_WEBHOOK_SECRET" ]]; then
    GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 20)
    echo "    Auto-generated webhook secret."
  fi

  read -r -p "Use Claude Max subscription? [y/N] " use_max
  ANTHROPIC_API_KEY=""
  CLAUDE_USE_MAX="false"
  if [[ "$use_max" == "y" || "$use_max" == "Y" ]]; then
    CLAUDE_USE_MAX="true"
  else
    read -r -s -p "ANTHROPIC_API_KEY: " ANTHROPIC_API_KEY
    echo ""
  fi

  read -r -s -p "SLACK_WEBHOOK_URL (optional, press Enter to skip): " SLACK_WEBHOOK_URL
  echo ""
  echo ""

  echo "==> Storing secrets in SSM Parameter Store"
  local ssm_params=(
    "GITHUB_TOKEN:${GITHUB_TOKEN}"
    "GITHUB_REPO:${REPO}"
    "GITHUB_WEBHOOK_SECRET:${GITHUB_WEBHOOK_SECRET}"
    "ANTHROPIC_API_KEY:${ANTHROPIC_API_KEY}"
    "CLAUDE_USE_MAX:${CLAUDE_USE_MAX}"
    "SLACK_WEBHOOK_URL:${SLACK_WEBHOOK_URL}"
  )

  for param in "${ssm_params[@]}"; do
    local key="${param%%:*}"
    local value="${param#*:}"
    aws ssm put-parameter \
      --name "/claude-agent/${slug}/${key}" \
      --type SecureString \
      --value "${value:-_empty_}" \
      --overwrite \
      --tags "Key=Project,Value=$PROJECT" "Key=TargetRepo,Value=$REPO" \
      >/dev/null 2>&1 || \
    aws ssm put-parameter \
      --name "/claude-agent/${slug}/${key}" \
      --type SecureString \
      --value "${value:-_empty_}" \
      --overwrite \
      >/dev/null
    echo "    Stored /claude-agent/${slug}/${key}"
  done

  # --- Check IAM instance profile ---
  if ! aws iam get-instance-profile --instance-profile-name "$IAM_PROFILE" &>/dev/null; then
    echo ""
    echo "Error: IAM instance profile '$IAM_PROFILE' not found." >&2
    echo "Run: bash scripts/aws-iam-bootstrap.sh --region $REGION" >&2
    exit 1
  fi

  # --- Resolve AMI ---
  echo ""
  echo "==> Resolving latest Amazon Linux 2023 AMI"
  local ami_id
  ami_id=$(resolve_ami)
  echo "    AMI: $ami_id"

  # --- Security Group ---
  echo ""
  echo "==> Creating security group"
  local sg_id
  sg_id=$(find_security_group "$slug")
  if [[ -n "$sg_id" ]]; then
    echo "    Using existing security group: $sg_id"
  else
    sg_id=$(aws ec2 create-security-group \
      --group-name "${PROJECT}-${slug}" \
      --description "claude-agent for ${REPO}" \
      --tag-specifications "$(tags_spec security-group)" \
      --query 'GroupId' --output text)
    echo "    Created: $sg_id"

    aws ec2 authorize-security-group-ingress --group-id "$sg_id" \
      --protocol tcp --port 5000 --cidr 0.0.0.0/0 >/dev/null
    aws ec2 authorize-security-group-ingress --group-id "$sg_id" \
      --protocol tcp --port 22 --cidr 0.0.0.0/0 >/dev/null
    echo "    Opened ports: 22 (SSH), 5000 (webhook)"
  fi

  # --- Key Pair ---
  echo ""
  echo "==> Creating key pair"
  local key_name="${PROJECT}-${slug}"
  if [[ -f "$key_file" ]]; then
    echo "    Key file already exists: $key_file"
  else
    # Delete remote key pair if it exists (orphaned from previous deploy)
    aws ec2 delete-key-pair --key-name "$key_name" 2>/dev/null || true
    aws ec2 create-key-pair \
      --key-name "$key_name" \
      --query 'KeyMaterial' --output text > "$key_file"
    chmod 600 "$key_file"
    echo "    Saved to: $key_file"
  fi

  # --- Generate user-data from template ---
  echo ""
  echo "==> Preparing cloud-init script"
  local userdata_file
  userdata_file=$(mktemp)
  sed \
    -e "s|{{REPO_SLUG}}|${slug}|g" \
    -e "s|{{AWS_REGION}}|${REGION}|g" \
    -e "s|{{AGENT_IMAGE}}|${AGENT_IMAGE}|g" \
    "${SCRIPT_DIR}/cloud-init.sh" > "$userdata_file"

  # --- Launch EC2 ---
  echo ""
  echo "==> Launching EC2 instance"
  local instance_id
  instance_id=$(aws ec2 run-instances \
    --image-id "$ami_id" \
    --instance-type "$INSTANCE_TYPE" \
    --key-name "$key_name" \
    --security-group-ids "$sg_id" \
    --iam-instance-profile Name="$IAM_PROFILE" \
    --user-data "file://${userdata_file}" \
    --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":20,"VolumeType":"gp3"}}]' \
    --tag-specifications "$(tags_spec instance)" "$(tags_spec volume)" \
    --query 'Instances[0].InstanceId' --output text)
  rm -f "$userdata_file"
  echo "    Instance: $instance_id"

  echo "    Waiting for instance to be running..."
  aws ec2 wait instance-running --instance-ids "$instance_id"
  echo "    Instance is running."

  # --- Elastic IP ---
  echo ""
  echo "==> Allocating Elastic IP"
  local alloc_id public_ip
  alloc_id=$(aws ec2 allocate-address \
    --domain vpc \
    --tag-specifications "$(tags_spec elastic-ip)" \
    --query 'AllocationId' --output text)
  aws ec2 associate-address \
    --instance-id "$instance_id" \
    --allocation-id "$alloc_id" >/dev/null
  public_ip=$(aws ec2 describe-addresses \
    --allocation-ids "$alloc_id" \
    --query 'Addresses[0].PublicIp' --output text)
  echo "    Public IP: $public_ip"

  # --- Wait for health ---
  echo ""
  wait_for_health "$public_ip" || true

  # --- Summary ---
  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║              claude-agent deployed successfully             ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""
  echo "  Repo:          $REPO"
  echo "  Instance:      $instance_id ($INSTANCE_TYPE)"
  echo "  Public IP:     $public_ip"
  echo "  Region:        $REGION"
  echo ""
  echo "  Webhook URL:   http://${public_ip}:5000/webhook/github"
  echo "  Health check:  curl http://${public_ip}:5000/health"
  echo "  Status:        curl http://${public_ip}:5000/status"
  echo ""
  echo "  SSH:           ssh -i $key_file ec2-user@${public_ip}"
  echo "  Logs:          ssh -i $key_file ec2-user@${public_ip} 'cd /opt/claude-agent && docker compose logs -f'"
  echo ""
  echo "  Webhook secret: ${GITHUB_WEBHOOK_SECRET}"
  echo ""
  echo "  Next steps:"
  echo "    1. Go to https://github.com/${REPO}/settings/hooks/new"
  echo "    2. Payload URL:  http://${public_ip}:5000/webhook/github"
  echo "    3. Content type: application/json"
  echo "    4. Secret:       (shown above)"
  echo "    5. Events:       Issues, Issue comments, Pushes"
  echo ""
}

cmd_update() {
  require_repo
  verify_aws_identity

  local slug=$(repo_slug "$REPO")
  local key_file=$(ssh_key_path)
  local instance_id=$(find_instance "$slug")

  if [[ -z "$instance_id" ]]; then
    echo "Error: No running instance found for $REPO" >&2
    exit 1
  fi

  local eip_json=$(find_elastic_ip)
  local public_ip=$(get_elastic_ip_address "$eip_json")

  if [[ -z "$public_ip" ]]; then
    public_ip=$(aws ec2 describe-instances \
      --instance-ids "$instance_id" \
      --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
  fi

  echo "==> Updating claude-agent on $public_ip ($instance_id)"
  ssh -o StrictHostKeyChecking=no -i "$key_file" "ec2-user@${public_ip}" \
    "cd /opt/claude-agent && sudo docker compose pull && sudo docker compose up -d"
  echo "==> Update complete."
}

cmd_teardown() {
  require_repo
  verify_aws_identity

  local slug=$(repo_slug "$REPO")

  echo "==> Tearing down claude-agent for: $REPO"
  echo ""
  read -r -p "This will DESTROY all AWS resources for this deployment. Continue? [y/N] " confirm
  if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Aborted."
    exit 0
  fi

  # Terminate instance
  local instance_id=$(find_instance "$slug")
  if [[ -n "$instance_id" ]]; then
    echo "    Terminating instance: $instance_id"
    aws ec2 terminate-instances --instance-ids "$instance_id" >/dev/null
    echo "    Waiting for termination..."
    aws ec2 wait instance-terminated --instance-ids "$instance_id"
    echo "    Instance terminated."
  else
    echo "    No running instance found."
  fi

  # Release Elastic IP
  local eip_json=$(find_elastic_ip)
  local alloc_id=$(get_elastic_ip_alloc_id "$eip_json")
  if [[ -n "$alloc_id" ]]; then
    echo "    Releasing Elastic IP: $(get_elastic_ip_address "$eip_json")"
    aws ec2 release-address --allocation-id "$alloc_id"
  fi

  # Delete security group (may need retries while ENIs detach)
  local sg_id=$(find_security_group "$slug")
  if [[ -n "$sg_id" ]]; then
    echo "    Deleting security group: $sg_id"
    local sg_attempts=0
    while [[ $sg_attempts -lt 12 ]]; do
      if aws ec2 delete-security-group --group-id "$sg_id" 2>/dev/null; then
        echo "    Security group deleted."
        break
      fi
      sg_attempts=$((sg_attempts + 1))
      echo "    Waiting for ENIs to detach (attempt $sg_attempts/12)..."
      sleep 10
    done
  fi

  # Delete key pair
  local key_name="${PROJECT}-${slug}"
  local key_file=$(ssh_key_path)
  aws ec2 delete-key-pair --key-name "$key_name" 2>/dev/null || true
  if [[ -f "$key_file" ]]; then
    rm -f "$key_file"
    echo "    Deleted key pair: $key_name"
  fi

  # Delete SSM parameters
  echo "    Deleting SSM parameters"
  local params
  params=$(aws ssm describe-parameters \
    --parameter-filters "Key=Name,Option=BeginsWith,Values=/claude-agent/${slug}/" \
    --query 'Parameters[].Name' --output text 2>/dev/null || true)
  if [[ -n "$params" ]]; then
    # shellcheck disable=SC2086
    aws ssm delete-parameters --names $params >/dev/null
    echo "    Deleted SSM parameters for /claude-agent/${slug}/"
  fi

  echo ""
  echo "==> Teardown complete for $REPO"
}

cmd_status() {
  verify_aws_identity

  if [[ -n "$REPO" ]]; then
    # Status for a specific repo
    local slug=$(repo_slug "$REPO")
    local instance_id=$(find_instance "$slug")
    if [[ -z "$instance_id" ]]; then
      echo "No claude-agent instance found for $REPO"
      return
    fi

    local eip_json=$(find_elastic_ip)
    local public_ip=$(get_elastic_ip_address "$eip_json")

    local instance_state
    instance_state=$(aws ec2 describe-instances \
      --instance-ids "$instance_id" \
      --query 'Reservations[0].Instances[0].State.Name' --output text)

    echo "claude-agent — $REPO"
    echo "  Instance: $instance_id ($instance_state)"
    echo "  IP:       ${public_ip:-none}"
    if [[ -n "$public_ip" ]]; then
      echo "  Webhook:  http://${public_ip}:5000/webhook/github"
      echo -n "  Health:   "
      curl -sf "http://${public_ip}:5000/health" 2>/dev/null || echo "unreachable"
    fi
  else
    # List all claude-agent instances
    echo "==> All claude-agent deployments in $REGION"
    echo ""
    aws ec2 describe-instances \
      --filters \
        "Name=tag:Project,Values=$PROJECT" \
        "Name=instance-state-name,Values=running,pending,stopped" \
      --query 'Reservations[].Instances[].{
        Id:InstanceId,
        State:State.Name,
        Type:InstanceType,
        Repo:Tags[?Key==`TargetRepo`].Value|[0],
        IP:PublicIpAddress
      }' --output table
  fi
}

cmd_help() {
  cat <<'EOF'
claude-agent AWS deployment tool

Usage:
  aws-deploy.sh <command> [options]

Commands:
  create     Deploy a new instance
  update     Pull latest image and restart
  teardown   Destroy all resources for a deployment
  status     Show deployment status

Options:
  --repo owner/repo       Target GitHub repository (required for create/update/teardown)
  --region us-east-1      AWS region (default: us-east-1)
  --instance-type t3.small  EC2 instance type (default: t3.small)
  --image ghcr.io/...     Docker image to deploy (default: derived from --repo)

Prerequisites:
  1. AWS CLI configured (aws configure)
  2. IAM bootstrap done (bash scripts/aws-iam-bootstrap.sh)

Examples:
  bash scripts/aws-deploy.sh create --repo myorg/myrepo
  bash scripts/aws-deploy.sh update --repo myorg/myrepo
  bash scripts/aws-deploy.sh status
  bash scripts/aws-deploy.sh teardown --repo myorg/myrepo
EOF
}

# ─── Dispatch ───

case "$COMMAND" in
  create)   cmd_create ;;
  update)   cmd_update ;;
  teardown) cmd_teardown ;;
  status)   cmd_status ;;
  help|--help|-h) cmd_help ;;
  *) echo "Unknown command: $COMMAND"; cmd_help; exit 1 ;;
esac
