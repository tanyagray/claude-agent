#!/bin/bash
# Create a test task file in the pending/ directory.
# Usage: ./scripts/create-task.sh <issue_number> <title> [body]
#
# Examples:
#   ./scripts/create-task.sh 42 "Add dark mode support"
#   ./scripts/create-task.sh 7 "Fix login bug" "The login form crashes when email has a + in it"

set -e

ISSUE_NUMBER="${1:?Usage: create-task.sh <issue_number> <title> [body]}"
TITLE="${2:?Usage: create-task.sh <issue_number> <title> [body]}"
BODY="${3:-$TITLE}"

# Find the tasks directory — check docker volume first, fall back to local
if docker compose ps --quiet claude-agent 2>/dev/null | head -1 | grep -q .; then
    TASKS_DIR=$(docker inspect --format '{{ range .Mounts }}{{ if eq .Destination "/data" }}{{ .Source }}{{ end }}{{ end }}' \
        "$(docker compose ps --quiet claude-agent)" 2>/dev/null)/tasks
fi

if [ -z "$TASKS_DIR" ] || [ ! -d "$TASKS_DIR" ]; then
    TASKS_DIR="${TASKS_DIR_LOCAL:-./tmp/tasks}"
    mkdir -p "$TASKS_DIR/pending" "$TASKS_DIR/in_progress" "$TASKS_DIR/completed" "$TASKS_DIR/failed"
fi

TIMESTAMP=$(date +%s)
SLUG=$(echo "$TITLE" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | head -c 30)
TASK_ID="${TIMESTAMP}-github_issue-${ISSUE_NUMBER}"
FILENAME="${TASK_ID}.md"
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

cat > "$TASKS_DIR/pending/$FILENAME" << EOF
---
id: "${TASK_ID}"
source: github_issue
event_type: issue_labeled
issue_number: ${ISSUE_NUMBER}
summary: "${TITLE}"
priority: 0
retries: 0
max_retries: 3
branch_name: null
pr_url: null
error_log: null
created_at: "${NOW}"
started_at: null
completed_at: null
---

## Issue Body

${BODY}
EOF

echo "Created task: $TASKS_DIR/pending/$FILENAME"
