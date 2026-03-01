#!/bin/bash
set -e

TASKS_DIR="${TASKS_DIR:-/data/tasks}"

# Create task subdirectories if they don't exist
mkdir -p "$TASKS_DIR/pending"
mkdir -p "$TASKS_DIR/in_progress"
mkdir -p "$TASKS_DIR/completed"
mkdir -p "$TASKS_DIR/failed"

echo "Task directories ready at $TASKS_DIR"
echo "Starting Claude Agent..."

exec supervisord -c /etc/supervisor/conf.d/claude-agent.conf
