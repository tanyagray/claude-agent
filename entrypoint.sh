#!/bin/bash
set -e

# Configure git identity from env vars.
# For GitHub App: set GITHUB_BOT_NAME and GITHUB_BOT_EMAIL in your .env
# (the setup-github-app script will output the correct values).
git config --global user.name "${GITHUB_BOT_NAME:-Claude Agent}"
git config --global user.email "${GITHUB_BOT_EMAIL:-claude-agent@noreply}"

TASKS_DIR="${TASKS_DIR:-/data/tasks}"

# Create task subdirectories if they don't exist
mkdir -p "$TASKS_DIR/pending"
mkdir -p "$TASKS_DIR/in_progress"
mkdir -p "$TASKS_DIR/completed"
mkdir -p "$TASKS_DIR/failed"

echo "Task directories ready at $TASKS_DIR"

export PORT="${PORT:-5000}"
echo "Starting Claude Agent on port $PORT..."

exec supervisord -c /etc/supervisor/conf.d/claude-agent.conf
