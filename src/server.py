"""FastAPI webhook server for receiving GitHub events."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response

from src import config
from src.tasks import TaskQueue

logger = logging.getLogger(__name__)

_shutting_down = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _shutting_down
    yield
    logger.info("Shutdown signal received, rejecting new webhooks")
    _shutting_down = True


app = FastAPI(title="Claude Agent Webhook Server", lifespan=lifespan)
task_queue = TaskQueue(config.TASKS_DIR)
_start_time = time.monotonic()


def _verify_signature(payload: bytes, signature: str | None) -> None:
    """Validate GitHub webhook signature (X-Hub-Signature-256)."""
    if not signature:
        raise HTTPException(status_code=401, detail="Missing signature header")

    expected = "sha256=" + hmac.new(
        config.GITHUB_WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(..., alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
) -> dict[str, str]:
    """Receive and route GitHub webhook events."""
    if _shutting_down:
        raise HTTPException(status_code=503, detail="Shutting down")

    body = await request.body()
    _verify_signature(body, x_hub_signature_256)

    payload: dict[str, Any] = await request.json()
    action = payload.get("action", "")

    if x_github_event == "issues":
        return _handle_issue_event(payload, action)
    elif x_github_event == "issue_comment":
        return _handle_comment_event(payload, action)
    elif x_github_event == "push":
        return _handle_push_event(payload)
    else:
        logger.debug("Ignoring event: %s", x_github_event)
        return {"status": "ignored"}


def _handle_issue_event(payload: dict[str, Any], action: str) -> dict[str, str]:
    """Handle 'issues' webhook — trigger on label addition."""
    if action != "labeled":
        return {"status": "ignored"}

    label = payload.get("label", {})
    if label.get("name") != config.TRIGGER_LABEL:
        return {"status": "ignored"}

    issue = payload["issue"]
    task_queue.create_task(
        source="github_issue",
        event_type="issue_labeled",
        summary=issue["title"],
        body=issue.get("body") or "",
        issue_number=issue["number"],
        max_retries=config.MAX_RETRIES_PER_TASK,
    )
    logger.info("Queued task for issue #%d: %s", issue["number"], issue["title"])
    return {"status": "accepted"}


def _handle_comment_event(payload: dict[str, Any], action: str) -> dict[str, str]:
    """Handle 'issue_comment' webhook — trigger on /claude commands."""
    if action != "created":
        return {"status": "ignored"}

    comment_body: str = payload.get("comment", {}).get("body", "")
    if not comment_body.startswith("/claude"):
        return {"status": "ignored"}

    # Strip the /claude prefix to get additional instructions
    extra_instructions = comment_body.removeprefix("/claude").strip()

    issue = payload["issue"]
    task_queue.create_task(
        source="github_issue",
        event_type="issue_comment_command",
        summary=issue["title"],
        body=issue.get("body") or "",
        issue_number=issue["number"],
        additional_context=extra_instructions or None,
        max_retries=config.MAX_RETRIES_PER_TASK,
    )
    logger.info("Queued /claude task for issue #%d", issue["number"])
    return {"status": "accepted"}


def _handle_push_event(payload: dict[str, Any]) -> dict[str, str]:
    """Handle 'push' webhook — trigger on docs/ changes to default branch."""
    ref = payload.get("ref", "")
    default_branch = payload.get("repository", {}).get("default_branch", "main")
    if ref != f"refs/heads/{default_branch}":
        return {"status": "ignored"}

    # Collect all changed files under docs/
    changed_docs: list[str] = []
    for commit in payload.get("commits", []):
        for file_list in (commit.get("added", []), commit.get("modified", [])):
            for f in file_list:
                if f.startswith("docs/") and f not in changed_docs:
                    changed_docs.append(f)

    if not changed_docs:
        return {"status": "ignored"}

    summary = f"Docs updated: {', '.join(changed_docs[:5])}"
    if len(changed_docs) > 5:
        summary += f" (+{len(changed_docs) - 5} more)"

    body = "The following documentation files were updated:\n\n"
    body += "\n".join(f"- `{f}`" for f in changed_docs)

    task_queue.create_task(
        source="docs_updated",
        event_type="push",
        summary=summary,
        body=body,
        max_retries=config.MAX_RETRIES_PER_TASK,
    )
    logger.info("Queued docs update task: %s", summary)
    return {"status": "accepted"}


@app.get("/status")
async def status() -> dict[str, Any]:
    """Return task counts and lists by status."""
    today = datetime.now(timezone.utc).date().isoformat()

    pending = task_queue.list_tasks("pending")
    in_progress = task_queue.list_tasks("in_progress")
    completed = task_queue.list_tasks("completed")
    failed = task_queue.list_tasks("failed")

    completed_today = [
        t for t in completed if t.completed_at and t.completed_at.startswith(today)
    ]

    def _task_summary(t):
        return {
            "id": t.id,
            "summary": t.summary,
            "issue_number": t.issue_number,
            "retries": t.retries,
            "created_at": t.created_at,
        }

    return {
        "pending": len(pending),
        "in_progress": len(in_progress),
        "completed_today": len(completed_today),
        "failed": len(failed),
        "tasks": {
            "pending": [_task_summary(t) for t in pending],
            "in_progress": [_task_summary(t) for t in in_progress],
        },
    }


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    uptime_seconds = int(time.monotonic() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return {
        "status": "ok",
        "uptime": f"{hours}h {minutes}m {seconds}s",
    }
