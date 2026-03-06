#!/bin/bash
# Cloud-init / user-data script for claude-agent EC2 instances.
# Placeholders replaced by aws-deploy.sh before use:
#   {{REPO_SLUG}}       — e.g. myorg-myrepo
#   {{AWS_REGION}}       — e.g. us-east-1
#   {{AGENT_IMAGE}}      — e.g. ghcr.io/owner/claude-agent:latest

set -euo pipefail
exec > /var/log/claude-agent-init.log 2>&1

REPO_SLUG="{{REPO_SLUG}}"
AWS_REGION="{{AWS_REGION}}"
AGENT_IMAGE="{{AGENT_IMAGE}}"
INSTALL_DIR="/opt/claude-agent"

echo "==> claude-agent cloud-init starting"
echo "    REPO_SLUG=$REPO_SLUG"
echo "    REGION=$AWS_REGION"
echo "    IMAGE=$AGENT_IMAGE"

# --- Install Docker ---
echo "==> Installing Docker"
dnf install -y docker
systemctl enable docker
systemctl start docker
usermod -aG docker ec2-user

# --- Install Docker Compose plugin ---
echo "==> Installing Docker Compose"
mkdir -p /usr/local/lib/docker/cli-plugins
ARCH=$(uname -m)
curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${ARCH}" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# --- Fetch secrets from SSM ---
echo "==> Fetching secrets from SSM Parameter Store"
fetch_param() {
  aws ssm get-parameter \
    --region "$AWS_REGION" \
    --name "/claude-agent/${REPO_SLUG}/$1" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || echo ""
}

GITHUB_TOKEN=$(fetch_param GITHUB_TOKEN)
GITHUB_REPO=$(fetch_param GITHUB_REPO)
GITHUB_WEBHOOK_SECRET=$(fetch_param GITHUB_WEBHOOK_SECRET)
ANTHROPIC_API_KEY=$(fetch_param ANTHROPIC_API_KEY)
CLAUDE_USE_MAX=$(fetch_param CLAUDE_USE_MAX)
SLACK_WEBHOOK_URL=$(fetch_param SLACK_WEBHOOK_URL)

# --- Write .env ---
echo "==> Writing configuration"
mkdir -p "$INSTALL_DIR"

cat > "${INSTALL_DIR}/.env" << EOF
GITHUB_TOKEN=${GITHUB_TOKEN}
GITHUB_REPO=${GITHUB_REPO}
GITHUB_WEBHOOK_SECRET=${GITHUB_WEBHOOK_SECRET}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
CLAUDE_USE_MAX=${CLAUDE_USE_MAX}
SLACK_WEBHOOK_URL=${SLACK_WEBHOOK_URL}
REPO_DIR=/data/repo
TASKS_DIR=/data/tasks
PORT=5000
EOF

chmod 600 "${INSTALL_DIR}/.env"

# --- Write docker-compose.yml ---
cat > "${INSTALL_DIR}/docker-compose.yml" << COMPOSE
services:
  claude-agent:
    image: ${AGENT_IMAGE}
    ports:
      - "5000:5000"
    volumes:
      - agent-data:/data
    env_file:
      - .env
    restart: unless-stopped

volumes:
  agent-data:
COMPOSE

# --- Login to ghcr.io ---
echo "==> Logging in to ghcr.io"
echo "$GITHUB_TOKEN" | docker login ghcr.io -u "claude-agent" --password-stdin

# --- Start the agent ---
echo "==> Starting claude-agent"
cd "$INSTALL_DIR"
docker compose pull
docker compose up -d

echo "==> cloud-init complete"
