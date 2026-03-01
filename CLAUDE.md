# Claude Agent

Autonomous Claude Code agent that watches GitHub issues/webhooks and opens PRs.

## Project Structure

- `src/server.py` — FastAPI webhook server (port 5000)
- `src/worker.py` — Main worker loop, invokes Claude Code CLI
- `src/tasks.py` — File-based task queue (markdown files with YAML frontmatter)
- `src/config.py` — Central config from env vars
- `src/github_api.py` — GitHub REST API helpers
- `src/notify.py` — Slack notifications

## Running Locally

### Start the agent

```bash
docker compose up -d
docker compose logs -f
```

### Expose webhooks with ngrok

When asked to "start ngrok", do the following:

1. Find the host port the webhook server is mapped to:
   ```bash
   docker compose port claude-agent 5000
   ```
   This returns something like `0.0.0.0:5000` — extract the port number.

2. Start ngrok on that port:
   ```bash
   ngrok http <port>
   ```

3. Report the public URL to the user (they need to set it as the GitHub webhook payload URL with `/webhook/github` appended).

If ngrok is not installed: `brew install ngrok` (macOS).
If auth is needed: `ngrok config add-authtoken <token>` — token is at https://dashboard.ngrok.com/get-started/your-authtoken

### Test without webhooks

Use the `/create-task` slash command (defined in `.claude/commands/create-task.md`) to create a test task. It runs `scripts/create-task.sh` which writes a properly formatted task file into the `pending/` directory. The worker picks it up automatically if the container is running.

## Useful Commands

```bash
curl http://localhost:5000/health       # health check
curl http://localhost:5000/status       # task queue status
docker compose logs -f                  # live logs
```
