# Claude Agent

An autonomous Claude Code agent for personal side projects. It runs as a Docker container, watches for GitHub Issue updates and documentation changes via webhooks, then autonomously implements the code using the Claude Code CLI and opens PRs — no human prompting needed.

The Docker image is published to GitHub Container Registry (ghcr.io) so you can deploy multiple copies across different repos/machines.

## How It Works

1. You write a detailed GitHub issue describing what you want built
2. You add the `claude` label to the issue
3. The agent picks it up, reads your codebase + docs, and implements it
4. A PR is opened automatically — you review and merge

```
GitHub Issue → Webhook → Task Queue → Claude Code CLI → PR
```

## Quick Start

```bash
git clone https://github.com/you/claude-agent.git
cd claude-agent
cp .env.example .env
# Edit .env with your values
docker compose up -d
```

## Deploy to EC2

1. Launch a **t3.medium** (or larger), Ubuntu 24.04, 20GB+ disk
2. Install Docker and Docker Compose:
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   ```
3. Pull the image and run:
   ```bash
   docker pull ghcr.io/you/claude-agent:latest
   mkdir claude-agent && cd claude-agent
   # Create .env with your values
   docker compose up -d
   ```
4. Open port **5000** in your EC2 security group (or put behind a reverse proxy with HTTPS)

## Run Multiple Instances

Each instance gets its own `.env` with a different `GITHUB_REPO`. Use different host ports and named volumes:

```yaml
# docker-compose.multi.yml
services:
  agent-repo-a:
    image: ghcr.io/you/claude-agent:latest
    ports:
      - "5001:5000"
    volumes:
      - repo-a-data:/data
    env_file:
      - .env.repo-a
    restart: unless-stopped

  agent-repo-b:
    image: ghcr.io/you/claude-agent:latest
    ports:
      - "5002:5000"
    volumes:
      - repo-b-data:/data
    env_file:
      - .env.repo-b
    restart: unless-stopped

volumes:
  repo-a-data:
  repo-b-data:
```

## Set Up GitHub Webhook

1. Go to your repo → **Settings** → **Webhooks** → **Add webhook**
2. **Payload URL**: `http://your-server:5000/webhook/github`
3. **Content type**: `application/json`
4. **Secret**: same value as `GITHUB_WEBHOOK_SECRET` in your `.env`
5. **Events**: Select **Issues**, **Issue comments**, and **Pushes**

## Create the Trigger Label

Create a label called `claude` in your GitHub repo (Issues → Labels → New label). This is what triggers the agent.

## Usage

### Basic: Label an Issue

1. Write a detailed GitHub issue (the more context, the better)
2. Add the `claude` label
3. The agent picks it up and opens a PR
4. Review the PR and merge

### Re-trigger: Comment `/claude`

If you want the agent to try again with different instructions:

```
/claude try a different approach — use React hooks instead of class components
```

### Docs Updates

When you push changes to files in `docs/` on your default branch, the agent will pick them up and apply any relevant code changes.

## Claude Code Billing

Pick one option in your `.env`:

| Option | Config | Best For |
|--------|--------|----------|
| **API billing** | `CLAUDE_USE_MAX=false` + set `ANTHROPIC_API_KEY` | Light use, pay-per-token |
| **Max plan** | `CLAUDE_USE_MAX=true` | Heavy use with existing Max subscription |

If using Max plan, you need to run `claude login` inside the container first:

```bash
docker compose exec claude-agent claude login
```

## Monitoring

```bash
# Live logs
docker compose logs -f

# Task status
curl http://localhost:5000/status

# Health check
curl http://localhost:5000/health

# Browse tasks directly
ls /var/lib/docker/volumes/claude-agent_agent-data/_data/tasks/
```

## Task Queue

Tasks are markdown files with YAML frontmatter stored in `/data/tasks/`:

```
/data/tasks/
├── pending/          # Waiting to be picked up
├── in_progress/      # Currently being worked on
├── completed/        # Successfully finished
└── failed/           # Failed after max retries
```

Each file contains the issue details, retry count, branch name, PR URL, and error logs. Fully transparent — just `ls` the directory.

## Tips for Writing Good Issues

The agent works best with detailed, specific issues:

- **Be specific**: "Add a `/health` endpoint that returns `{status: ok}`" beats "add health check"
- **Include acceptance criteria**: What should the result look like?
- **Reference existing code**: "Similar to how `UserService` works in `src/services/`"
- **Mention test expectations**: "Should pass with `npm test`"
- **Link to docs**: If there's an API or library involved, link the docs

## Guardrails

- **Never pushes to main** — always feature branches + PRs
- **Max retries** — configurable (default 3), gives up and notifies Slack
- **Timeout** — kills Claude Code CLI if it exceeds `CLAUDE_TIMEOUT` (default 30 min)
- **Webhook signature validation** — rejects invalid requests
- **Single task at a time** — sequential worker, no race conditions
- **File-based queue** — fully transparent, `ls` the tasks directory
- **Blocked state** — Claude reports blockers instead of making bad changes
- **Label tracking** — `claude`, `claude-pr-open`, `claude-blocked`

## Debugging

### Agent isn't picking up issues
- Check the webhook is configured correctly: `curl http://your-server:5000/health`
- Verify the `claude` label exists and was just added (not already present)
- Check logs: `docker compose logs -f`

### Claude produces no changes
- The issue description may be too vague — add more detail
- Check the task file in `failed/` for error logs
- The repo may need a `docs/` directory with project context

### Claude is blocked
- Check the issue for a comment explaining what it needs
- Look for the `claude-blocked` label
- Provide the missing info and re-trigger with `/claude`

## Project Structure

```
claude-agent/
├── Dockerfile
├── docker-compose.yml
├── supervisord.conf
├── entrypoint.sh
├── requirements.txt
├── .github/
│   └── workflows/
│       └── publish.yml         # Build and push to ghcr.io
├── src/
│   ├── server.py               # FastAPI webhook server
│   ├── worker.py               # Main worker loop
│   ├── config.py               # Central config from env vars
│   ├── tasks.py                # File-based task queue
│   ├── github_api.py           # GitHub REST API helpers
│   └── notify.py               # Slack notifications
├── .env.example
└── README.md
```
