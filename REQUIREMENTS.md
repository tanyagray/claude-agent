# Claude Agent — Feature Requirements

> Aspirational feature spec organized by user journey.
> Each requirement has **Verify** criteria that Claude can check by inspecting the codebase.

---

## 1. Trigger

How work enters the system.

- [ ] **GitHub Label Trigger**
  _Adding a configurable label (default: `claude`) to a GitHub issue creates a task for the agent._
  - **Verify**: `src/server.py` handles `issues.labeled` webhook events and calls a task-creation function when the label matches `config.TRIGGER_LABEL`.

- [ ] **Slash-Command Trigger**
  _A `/claude` comment on any issue triggers the agent, with the comment body passed as additional instructions._
  - **Verify**: `src/server.py` handles `issue_comment.created` events, parses comments starting with `/claude`, and passes the remaining text as `additional_instructions` to the task.

- [ ] **Docs-Driven Trigger**
  _Pushing changes to `docs/` on the default branch triggers the agent to apply documentation-driven changes._
  - **Verify**: `src/server.py` handles `push` events and filters for commits touching files under `docs/`.

- [ ] **Slack Trigger**
  _A message in a designated Slack channel or DM to the bot creates a task, with threaded replies for status updates._
  - **Verify**: A Slack event handler exists (e.g., `src/slack_handler.py` or equivalent) that receives Slack messages, creates tasks via `src/tasks.py`, and posts threaded replies with task status.

- [ ] **Startup Sync**
  _On startup, the agent scans for open issues with the trigger label and queues any that don't already have tasks._
  - **Verify**: `src/worker.py` or `src/server.py` calls a function at startup that queries GitHub for open issues with the trigger label and creates tasks for unprocessed ones.

- [ ] **Self-Created Sub-Issues**
  _When Claude breaks an epic into subtasks, each sub-issue it creates can trigger itself back into the system._
  - **Verify**: `src/github_api.py` has a function to create GitHub issues with the trigger label, and the webhook handler processes these the same as human-created issues.

---

## 2. Plan

Claude proposes an approach before writing code.

- [ ] **Plan Comment**
  _Before implementing, Claude posts a comment on the issue outlining its proposed approach: what files it will change, what the high-level strategy is, and any assumptions._
  - **Verify**: `src/worker.py` has a planning phase that calls `src/github_api.py` to post a structured comment on the issue before entering the implementation phase. The comment includes a plan summary.

- [ ] **Wait for Approval**
  _After posting the plan, Claude waits for human approval (e.g., a thumbs-up reaction or `/approve` comment) before starting implementation._
  - **Verify**: `src/worker.py` has a polling or webhook-based mechanism that checks for an approval signal (reaction or comment) on the plan comment. The task remains in a `planning` state until approval is received.

- [ ] **Plan Bypass for Small Tasks**
  _Issues with certain labels (e.g., `bug`, `typo`) skip the planning phase and go straight to implementation._
  - **Verify**: `src/worker.py` checks issue labels against a configurable list (e.g., `config.SKIP_PLAN_LABELS`) and skips the planning phase when matched.

- [ ] **Plan Revision**
  _If a human replies to the plan comment with feedback, Claude revises its plan and posts an updated comment before proceeding._
  - **Verify**: The planning phase in `src/worker.py` detects replies to its plan comment, re-runs planning with the feedback incorporated, and posts a revised plan.

---

## 3. Implement

Claude writes code and opens a PR.

- [ ] **Branch Creation**
  _Claude creates a feature branch from the default branch with a descriptive name including the issue number._
  - **Verify**: `src/worker.py` creates a branch matching the pattern `claude/issue-{number}-{slug}` from the repo's default branch.

- [ ] **Claude Code Invocation**
  _The agent invokes Claude Code CLI with the issue context, project documentation, and any additional instructions._
  - **Verify**: `src/worker.py` constructs a prompt from the issue body, additional instructions, and project context (e.g., `docs/` and `README.md` contents), then runs the `claude` CLI via subprocess.

- [ ] **Repo-Configured Quality Checks**
  _Before opening a PR, Claude detects and runs the repo's configured checks (tests, linting, formatting, type checking) and fixes any failures._
  - **Verify**: `src/worker.py` has a post-implementation step that discovers check commands (from `package.json` scripts, `Makefile` targets, CI config, or a `CLAUDE.md` directive) and runs them. If checks fail, Claude re-invokes itself with the failure output to fix the issues.

- [ ] **PR Creation**
  _Claude opens a pull request with a descriptive title, body referencing the issue, and a `Closes #N` reference._
  - **Verify**: `src/github_api.py` has a `create_pull_request` function that sets the title, body (with `Closes #{issue_number}`), and base/head branches.

- [ ] **Scope Enforcement**
  _Claude limits its changes to what's described in the issue. If it discovers out-of-scope work, it logs a follow-up ticket instead of making the change._
  - **Verify**: The prompt sent to Claude Code in `src/worker.py` includes explicit instructions to stay within scope. A function exists in `src/github_api.py` to create follow-up issues for out-of-scope discoveries.

- [ ] **Timeout Protection**
  _Claude Code is killed if it exceeds a configurable timeout (default: 30 minutes)._
  - **Verify**: `src/worker.py` sets a timeout on the Claude Code subprocess (configured via `config.CLAUDE_TIMEOUT`) and terminates the process if exceeded.

- [ ] **Epic Decomposition**
  _For large issues (e.g., labeled `epic`), Claude breaks the work into sub-issues, creates them on GitHub with the trigger label, and links them to the parent._
  - **Verify**: `src/worker.py` detects epic-labeled issues and enters a decomposition flow that calls `src/github_api.py` to create child issues with the trigger label and a reference to the parent issue.

---

## 4. Review

Claude responds to PR feedback and iterates.

- [ ] **Review Comment Handler**
  _When a reviewer leaves comments on Claude's PR, the agent picks them up and pushes fixes._
  - **Verify**: `src/server.py` handles `pull_request_review` and/or `pull_request_review_comment` webhook events. `src/worker.py` processes these by checking out the PR branch, invoking Claude Code with the review feedback, and pushing new commits.

- [ ] **Review Iteration**
  _Claude can go through multiple rounds of review, addressing each batch of comments in a new commit._
  - **Verify**: The review handler in `src/worker.py` can be triggered multiple times for the same PR, creating a new commit each time with a message referencing the review round.

- [ ] **Stuck Detection + Draft Conversion**
  _If Claude can't resolve a review comment after retrying, it converts the PR to draft, adds a label (e.g., `claude-needs-help`), and comments explaining what it's stuck on._
  - **Verify**: `src/worker.py` tracks review-fix attempts per PR. After exceeding a retry limit, it calls `src/github_api.py` to convert the PR to draft, add a label, and post a comment explaining the blocker.

- [ ] **PR Draft Conversion**
  _The GitHub API helper supports converting a PR to draft status._
  - **Verify**: `src/github_api.py` has a function (e.g., `convert_pr_to_draft`) that uses the GitHub GraphQL API to convert a PR to draft.

---

## 5. CI/CD

Claude monitors and fixes CI failures on its PRs.

- [ ] **CI Status Monitoring**
  _Claude watches for CI check results on its PRs (via webhooks or polling)._
  - **Verify**: `src/server.py` handles `check_suite.completed` or `check_run.completed` webhook events for PRs opened by Claude, or `src/worker.py` polls the GitHub Checks API.

- [ ] **Auto-Fix on Failure**
  _When CI fails on a Claude PR, the agent checks out the branch, invokes Claude Code with the failure logs, and pushes a fix._
  - **Verify**: `src/worker.py` has a CI-fix flow that fetches CI logs (from GitHub Actions API or similar), constructs a prompt with the failure output, runs Claude Code, and pushes the resulting fix.

- [ ] **Fix Notification**
  _After auto-fixing a CI failure, Claude comments on the PR explaining what broke and how it was fixed._
  - **Verify**: The CI-fix flow in `src/worker.py` calls `src/github_api.py` to post a comment on the PR with the failure summary and fix description.

- [ ] **CI Fix Retry Limit**
  _Claude won't loop forever on CI failures — after N attempts it stops and notifies._
  - **Verify**: `src/worker.py` tracks CI fix attempts per PR and stops after a configurable limit, posting a comment and notifying via Slack.

---

## 6. Merge & Cleanup

Post-merge housekeeping.

- [ ] **Human-Only Merging**
  _Claude never merges its own PRs. All merges require human approval._
  - **Verify**: No function in `src/github_api.py` or `src/worker.py` calls the GitHub merge API. The PR is opened and left for human review.

- [ ] **Branch Cleanup**
  _After a PR is merged (or closed), Claude deletes the feature branch._
  - **Verify**: `src/server.py` handles `pull_request.closed` events and calls a cleanup function that deletes the feature branch via `src/github_api.py` or git commands.

- [ ] **Label Management**
  _Claude manages labels throughout the lifecycle: adds `claude-pr-open` when PR is created, removes the trigger label, adds `claude-needs-help` when stuck._
  - **Verify**: `src/github_api.py` has functions to add and remove labels, called at appropriate points in `src/worker.py` (PR creation, stuck state, etc.).

- [ ] **Linked Ticket Updates**
  _If the issue is linked to a PM tool ticket (Linear/Jira), Claude updates the ticket status._
  - **Verify**: `src/worker.py` or a dedicated module (e.g., `src/pm_integration.py`) updates external ticket status (e.g., "In Review", "Done") at key lifecycle points.

---

## 7. Learn

Claude improves over time through codified knowledge.

- [ ] **CLAUDE.md Update Proposals**
  _When Claude notices recurring feedback patterns (e.g., "always run X before committing" or "prefer Y over Z"), it opens a PR to update the repo's `CLAUDE.md` with new guidelines._
  - **Verify**: `src/worker.py` has a learning flow that detects recurring themes in PR review comments and creates a PR modifying `CLAUDE.md` with proposed new rules or preferences.

- [ ] **Feedback Tracking**
  _Claude tracks review comments and human corrections across PRs to identify patterns._
  - **Verify**: A module (e.g., `src/feedback.py` or within `src/worker.py`) stores or aggregates review comment themes (e.g., in a local file or structured log) to inform CLAUDE.md update proposals.

---

## 8. Slack Integration

Full conversational Slack experience.

- [ ] **Task Creation from Slack**
  _Users can create tasks by messaging Claude in Slack (DM or channel mention), and Claude creates a corresponding GitHub issue._
  - **Verify**: A Slack handler (e.g., `src/slack_handler.py`) receives messages, creates a GitHub issue via `src/github_api.py`, and responds in the Slack thread with the issue link.

- [ ] **Threaded Progress Updates**
  _Claude posts progress updates in the Slack thread where the task was initiated (or in a configured channel)._
  - **Verify**: `src/notify.py` supports posting messages to a specific Slack thread (using `thread_ts`), and `src/worker.py` calls this at key lifecycle points.

- [ ] **Conversational Clarification**
  _Claude can ask clarifying questions in Slack threads and incorporate answers into the task._
  - **Verify**: The Slack handler supports back-and-forth: Claude posts a question, waits for a reply in the thread, and appends the reply to the task's additional instructions.

- [ ] **Rich Notifications**
  _Slack notifications include structured information: PR links, issue links, status, error summaries._
  - **Verify**: `src/notify.py` sends Slack messages with Block Kit formatting (or at minimum, markdown with links and structured sections), not just plain text.

---

## 9. Project Management

Integration with external project tracking tools.

- [ ] **Follow-Up Ticket Creation**
  _When Claude discovers out-of-scope work during implementation, it creates a follow-up ticket (GitHub issue, Linear, or Jira) and assigns it to itself for later._
  - **Verify**: `src/github_api.py` (or a PM integration module) has a function to create follow-up issues with the trigger label and a reference to the originating issue/PR.

- [ ] **Status Sync**
  _Claude updates external PM tool status as it progresses through the lifecycle (e.g., "In Progress", "In Review", "Blocked")._
  - **Verify**: A PM integration module exists that maps internal task states to external tool statuses and pushes updates at state transitions.

- [ ] **Self-Assignment for Later**
  _Claude can create tickets assigned to itself for work it wants to do later, or flag tickets needing human discussion before starting._
  - **Verify**: The ticket creation function supports setting an assignee and adding labels like `claude-needs-discussion` or `claude-later`.

---

## 10. Observability

Monitoring, logging, and debugging.

- [ ] **Health Check Endpoint**
  _`GET /health` returns a 200 response with uptime information._
  - **Verify**: `src/server.py` defines a `/health` route that returns a JSON response including at minimum an `uptime` field.

- [ ] **Task Queue Status Endpoint**
  _`GET /status` returns counts and details of pending, in-progress, completed, and failed tasks._
  - **Verify**: `src/server.py` defines a `/status` route that calls `src/tasks.py` to enumerate tasks by state and returns a JSON summary.

- [ ] **Structured Logging**
  _All log output uses structured logging (JSON format) with consistent fields: timestamp, level, task_id, issue_number._
  - **Verify**: `src/` files use a structured logging setup (e.g., `structlog`, `python-json-logger`, or a custom JSON formatter) with fields like `task_id` and `issue_number` in log entries.

- [ ] **Error Alerting**
  _Unhandled errors and task failures trigger Slack notifications with error details._
  - **Verify**: `src/worker.py` has exception handlers that call `src/notify.py` with error details (traceback, task context) on task failure.

---

## 11. Deployment & Operations

Running and scaling the agent.

- [ ] **Docker Compose Deployment**
  _The agent runs via `docker compose up` with a single service, persistent volume for task data, and environment-based configuration._
  - **Verify**: `docker-compose.yml` exists with a service definition, a named volume for `/data`, and environment variable references.

- [ ] **Graceful Shutdown**
  _On SIGTERM, the webhook server stops accepting new requests (returns 503) and the worker finishes its current task before exiting._
  - **Verify**: `src/server.py` has a shutdown handler that sets a flag to reject new webhooks with 503. `src/worker.py` catches SIGTERM and completes the current task before exiting.

- [ ] **Multi-Repo Support**
  _Multiple instances can run side-by-side, each targeting a different repository._
  - **Verify**: `docker-compose.yml` or a `docker-compose.multi.yml` supports multiple service definitions with separate env files and volumes.

- [ ] **Retry with Backoff**
  _Failed tasks are retried up to a configurable max, with increasing delay between attempts._
  - **Verify**: `src/tasks.py` or `src/worker.py` implements retry logic with a delay that increases per attempt (e.g., exponential backoff or linear delay), configurable via `config.MAX_RETRIES_PER_TASK`.

- [ ] **Container Image Publishing**
  _A CI workflow builds and publishes the Docker image on pushes to main._
  - **Verify**: `.github/workflows/publish.yml` (or similar) builds the Docker image and pushes it to a container registry (e.g., `ghcr.io`).

---

## 12. Guardrails & Safety

Ensuring Claude operates safely and within bounds.

- [ ] **Never Push to Main**
  _Claude always works on feature branches and never commits directly to the default branch._
  - **Verify**: `src/worker.py` creates a new branch before making any changes. No code path exists that commits or pushes to the default branch.

- [ ] **Webhook Signature Validation**
  _All incoming webhooks are validated with HMAC-SHA256 using the configured secret._
  - **Verify**: `src/server.py` validates the `X-Hub-Signature-256` header against the request body using `config.GITHUB_WEBHOOK_SECRET` before processing any event.

- [ ] **Scope Limits**
  _Claude refuses to make changes that are clearly out of scope for the issue it's working on._
  - **Verify**: The prompt constructed in `src/worker.py` includes explicit instructions to stay focused on the issue scope and create follow-up tickets for out-of-scope discoveries.

- [ ] **Plan-Before-Code**
  _For non-trivial issues, Claude must propose and get approval for its approach before writing code._
  - **Verify**: `src/worker.py` has a planning phase that runs before implementation for issues that don't match `config.SKIP_PLAN_LABELS`.

- [ ] **Blocked State**
  _If Claude determines it cannot complete a task, it explains why on the issue, labels it appropriately, and stops._
  - **Verify**: `src/worker.py` detects a blocked signal (e.g., a `CLAUDE_BLOCKED.md` file or explicit output), comments on the issue with the reason, and adds a `claude-blocked` label.

- [ ] **Configurable Timeout**
  _Claude Code execution is time-limited to prevent runaway processes._
  - **Verify**: `src/worker.py` uses `config.CLAUDE_TIMEOUT` (default 1800 seconds) to set a subprocess timeout, and kills the process if exceeded.
