<div align="center">

# Claude Agent

**Autonomous Claude Code agent that watches GitHub issues and opens PRs — no human prompting needed.**

[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue?logo=docker)](https://github.com/tanyagray/claude-agent/pkgs/container/claude-agent)
[![Build](https://github.com/tanyagray/claude-agent/actions/workflows/publish.yml/badge.svg)](https://github.com/tanyagray/claude-agent/actions/workflows/publish.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)

</div>

---

Claude Agent runs as a Docker container, listens for GitHub issue webhooks, implements the requested changes using the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code), and opens a PR — all without any human prompting.

```
GitHub Issue  ──►  Webhook  ──►  Task Queue  ──►  Claude Code CLI  ──►  Pull Request
```

---

## Table of Contents

- [Features](#features)
- [How It Works](#how-it-works)
- [Quick Start](#quick-start)
- [GitHub Setup](#github-setup)
  - [GitHub App (recommended)](#github-app-recommended)
  - [Personal Access Token](#personal-access-token)
- [Configuration](#configuration)
- [Deployment](#deployment)
  - [Local / Docker Compose](#local--docker-compose)
  - [Deploy to EC2](#deploy-to-ec2)
  - [Run Multiple Instances](#run-multiple-instances)
- [Set Up GitHub Webhook](#set-up-github-webhook)
- [Create the Trigger Label](#create-the-trigger-label)
- [Usage](#usage)
- [Claude Code Billing](#claude-code-billing)
- [Monitoring](#monitoring)
- [Task Queue](#task-queue)
- [Guardrails](#guardrails)
- [Tips for Writing Good Issues](#tips-for-writing-good-issues)
- [Debugging](#debugging)
- [Project Structure](#project-structure)
- [Contributing](#contributing)
- [License](#license)

---

## Features

- **Zero prompting** — label an issue and Claude handles the rest
- **Full code context** — reads your entire codebase before making changes
- **Automated PRs** — commits, branches, and pull requests created automatically
- **Retry logic** — configurable retries with Slack notifications on failure
- **Docs-driven changes** — push to `docs/` and the agent applies the changes
- **Re-triggerable** — comment `/claude` on any issue to re-run with new instructions
- **Transparent queue** — file-based task queue you can inspect with `ls`
- **Multi-repo** — run multiple instances for different repositories
- **Safe by default** — never pushes to `main`, always feature branches + PRs

---

## How It Works

1. **Write a detailed GitHub issue** describing what you want built
2. **Add the `claude` label** to the issue
3. **The agent reads your codebase** and implements the changes using Claude Code CLI
4. **A PR is opened automatically** — you review and merge

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                   │
│   GitHub Issue                                                    │
│   (labeled "claude")                                             │
│         │                                                         │
│         ▼                                                         │
│   Webhook Server  ──►  Task Queue  ──►  Worker                   │
│   (FastAPI :5000)      (file-based)     │                        │
│                                          │                        │
│                                          ▼                        │
│                                    Claude Code CLI               │
│                                    (reads repo + implements)      │
│                                          │                        │
│                                          ▼                        │
│                                    Git Push + PR                  │
│                                    (feature branch)               │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

```bash
git clone https://github.com/tanyagray/claude-agent.git
cd claude-agent
cp .env.example .env
# Edit .env with your values
docker compose up -d
```

Then complete the setup:

1. [Set up GitHub auth](#github-setup) — GitHub App (recommended) or PAT
2. [Set up your GitHub webhook](#set-up-github-webhook)
3. [Create the `claude` trigger label](#create-the-trigger-label)
4. Open an issue on your target repo, add the `claude` label, and watch it work

---

## GitHub Setup

The agent needs GitHub credentials to read issues, push branches, and open PRs. You have two options.

### GitHub App (recommended)

A GitHub App gives the agent its own distinct identity — commits, comments, and PR assignments appear as `your-app-name[bot]` rather than your personal account. It is the right choice for production use.

**Create the app automatically** using the included skill (requires `gh` CLI):

```
/setup-github-app
```

This opens GitHub in your browser, walks you through creating the app with the right permissions, installs it on your target repo, and prints the exact env vars to paste into your `.env`.

**What permissions the app requires:**

| Permission | Level | Why |
|------------|-------|-----|
| Contents | Read & write | Push feature branches |
| Issues | Read & write | Read issue body, add labels and comments |
| Pull requests | Read & write | Open PRs |
| Metadata | Read | Required by GitHub for all apps |
| Workflows | Read & write | Modify and trigger GitHub Actions workflows |

**After running the skill**, add these to your `.env`:

```env
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
GITHUB_APP_INSTALLATION_ID=78901234
GITHUB_BOT_NAME=your-app-name[bot]
GITHUB_BOT_EMAIL=123456+your-app-name[bot]@users.noreply.github.com
```

> **Workflows note:** By default, pull requests opened by a GitHub App do not automatically trigger CI. Enable this in your repo under **Settings → Actions → General → Allow GitHub Actions to create and approve pull requests**.

### Personal Access Token

Simpler to set up but commits will appear as your own GitHub account. Suitable for personal projects where that distinction doesn't matter.

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)**
2. Generate a new token with scopes: `repo`, `workflow`
3. Add to your `.env`:

```env
GITHUB_TOKEN=ghp_your-personal-access-token
```

Classic PATs with the `workflow` scope have no restrictions on triggering or modifying GitHub Actions workflows.

---

## Configuration

Copy `.env.example` to `.env` and fill in your values.

### Required

| Variable | Description |
|----------|-------------|
| `GITHUB_REPO` | Target repository in `owner/repo` format |
| `GITHUB_WEBHOOK_SECRET` | Random secret string for webhook validation |

### GitHub Auth (pick one — see [GitHub Setup](#github-setup))

| Variable | Description |
|----------|-------------|
| `GITHUB_APP_ID` | App ID from your GitHub App settings |
| `GITHUB_APP_PRIVATE_KEY` | PEM private key (newlines as `\n`) |
| `GITHUB_APP_INSTALLATION_ID` | Installation ID for your target repo |
| `GITHUB_BOT_NAME` | Display name for git commits (e.g. `myapp[bot]`) |
| `GITHUB_BOT_EMAIL` | Email for git commits (e.g. `123+myapp[bot]@users.noreply.github.com`) |
| `GITHUB_TOKEN` | PAT fallback — used if App vars are not set |

### Claude Code Billing (pick one)

| Option | Variables | Best For |
|--------|-----------|----------|
| **API billing** | `ANTHROPIC_API_KEY=sk-ant-...` + `CLAUDE_USE_MAX=false` | Light use, pay-per-token |
| **Max plan** | `CLAUDE_USE_MAX=true` (leave API key empty) | Heavy use with a Max subscription |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `SLACK_WEBHOOK_URL` | — | Slack notifications for completions and failures |
| `REPO_DIR` | `/data/repo` | Where the target repo is cloned |
| `TASKS_DIR` | `/data/tasks` | Task queue directory |
| `PORT` | `5000` | Port for the webhook server |
| `POLL_INTERVAL` | `60` | Seconds between polling cycles |
| `MAX_RETRIES_PER_TASK` | `3` | Max attempts before marking a task failed |
| `CLAUDE_TIMEOUT` | `1800` | Seconds before killing Claude Code (30 min) |
| `TRIGGER_LABEL` | `claude` | GitHub label that triggers the agent |

---

## Deployment

### Local / Docker Compose

```bash
docker compose up -d
docker compose logs -f
```

### Deploy to EC2

1. Launch a **t3.medium** (or larger), Ubuntu 24.04, 20 GB+ disk
2. Install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   ```
3. Pull the image and start the agent:
   ```bash
   docker pull ghcr.io/tanyagray/claude-agent:latest
   mkdir claude-agent && cd claude-agent
   # Create .env with your values
   docker compose up -d
   ```
4. Open port **5000** in your EC2 security group (or put it behind a reverse proxy with HTTPS)

### Run Multiple Instances

Each instance gets its own `.env` with a different `GITHUB_REPO`. Use different host ports and named volumes:

```yaml
# docker-compose.multi.yml
services:
  agent-repo-a:
    image: ghcr.io/tanyagray/claude-agent:latest
    ports:
      - "5001:5000"
    volumes:
      - repo-a-data:/data
    env_file:
      - .env.repo-a
    restart: unless-stopped

  agent-repo-b:
    image: ghcr.io/tanyagray/claude-agent:latest
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

---

## Set Up GitHub Webhook

1. Go to your target repo → **Settings** → **Webhooks** → **Add webhook**
2. **Payload URL**: `http://your-server:5000/webhook/github`
3. **Content type**: `application/json`
4. **Secret**: same value as `GITHUB_WEBHOOK_SECRET` in your `.env`
5. **Events**: Select **Issues**, **Issue comments**, and **Pushes**

> **Tip:** Use [ngrok](https://ngrok.com) to expose a local instance for testing:
> ```bash
> ngrok http 5000
> ```

---

## Create the Trigger Label

Create a label called `claude` in your target GitHub repo:

**Issues → Labels → New label** → name it `claude`

This is what triggers the agent when applied to an issue.

---

## Usage

### Label an Issue

1. Write a detailed GitHub issue (the more context, the better)
2. Add the `claude` label
3. The agent picks it up and opens a PR
4. Review and merge

### Re-trigger with a Comment

Comment `/claude` on an issue to have the agent try again with updated instructions:

```
/claude try a different approach — use React hooks instead of class components
```

### Docs-driven Changes

Push changes to files in `docs/` on your default branch and the agent will automatically pick them up and apply any relevant code changes.

---

## Claude Code Billing

When using the **Max plan**, log in once inside the container:

```bash
docker compose exec claude-agent claude login
```

Follow the browser prompts. The session is persisted in the named volume.

---

## Monitoring

```bash
# Live logs
docker compose logs -f

# Task queue status
curl http://localhost:5000/status

# Health check
curl http://localhost:5000/health

# Browse task files directly
ls /var/lib/docker/volumes/claude-agent_agent-data/_data/tasks/
```

---

## Task Queue

Tasks are Markdown files with YAML frontmatter stored in `/data/tasks/`:

```
/data/tasks/
├── pending/        # Waiting to be picked up
├── in_progress/    # Currently being worked on
├── completed/      # Successfully finished
└── failed/         # Failed after max retries
```

Each file contains the issue details, retry count, branch name, PR URL, and error logs — fully transparent, just `ls` the directory to inspect.

---

## Guardrails

| Guardrail | Details |
|-----------|---------|
| Never pushes to `main` | Always feature branches + PRs |
| Max retries | Configurable (default 3), gives up and notifies Slack |
| Timeout | Kills Claude Code CLI if it exceeds `CLAUDE_TIMEOUT` (default 30 min) |
| Webhook validation | Rejects requests with invalid HMAC signatures |
| Sequential worker | Single task at a time — no race conditions |
| Transparent queue | File-based tasks, `ls` the directory to inspect |
| Blocked state | Claude reports blockers instead of making bad changes |
| Label tracking | `claude`, `claude-pr-open`, `claude-blocked` |

---

## Tips for Writing Good Issues

The agent works best with detailed, specific issues:

- **Be specific** — "Add a `/health` endpoint that returns `{"status": "ok"}`" beats "add health check"
- **Include acceptance criteria** — what should the result look like?
- **Reference existing code** — "Similar to how `UserService` works in `src/services/`"
- **Mention test expectations** — "Should pass with `npm test`"
- **Link to docs** — if there's an API or library involved, link the docs
- **Describe the why** — context helps Claude make better architectural decisions

---

## Debugging

### Agent isn't picking up issues

- Check the webhook is configured correctly: `curl http://your-server:5000/health`
- Verify the `claude` label exists and was **just added** (not already present when the container started)
- Check logs: `docker compose logs -f`

### Claude produces no changes

- The issue description may be too vague — add more detail
- Check the task file in `failed/` for error logs
- The repo may need a `docs/` directory with project context

### Claude is blocked

- Check the issue for a comment explaining what it needs
- Look for the `claude-blocked` label on the issue
- Provide the missing information and re-trigger with `/claude`

---

## Project Structure

```
claude-agent/
├── Dockerfile
├── docker-compose.yml
├── supervisord.conf
├── entrypoint.sh
├── requirements.txt
├── .env.example
├── .github/
│   └── workflows/
│       └── publish.yml         # Build and push to ghcr.io
└── src/
    ├── server.py               # FastAPI webhook server
    ├── worker.py               # Main worker loop
    ├── config.py               # Central config from env vars
    ├── tasks.py                # File-based task queue
    ├── github_api.py           # GitHub REST API helpers
    └── notify.py               # Slack notifications
```

---

## Contributing

Contributions are welcome. Please open an issue describing the change before submitting a PR. The agent can even write the PR for you — just open a detailed issue and add the `claude` label.

---

## License

[MIT](LICENSE)
