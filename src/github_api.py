"""GitHub REST API helpers for PRs, issues, and labels."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

import httpx
import jwt

logger = logging.getLogger(__name__)

# Lazy import to avoid circular dependency at module level
_config = None

# Installation token cache (per-process; each process refreshes independently)
_installation_token: str = ""
_installation_token_expires: float = 0.0


def _get_config():
    global _config
    if _config is None:
        from src import config
        _config = config
    return _config


def _generate_jwt() -> str:
    """Generate a GitHub App JWT valid for 10 minutes."""
    cfg = _get_config()
    now = int(time.time())
    payload = {
        "iat": now - 60,   # issued-at: 60 s in the past to cover clock skew
        "exp": now + 600,  # expires: 10 minutes from now
        "iss": cfg.GITHUB_APP_ID,
    }
    return jwt.encode(payload, cfg.GITHUB_APP_PRIVATE_KEY, algorithm="RS256")


def _get_installation_token() -> str:
    """Return a valid GitHub App installation token, refreshing if needed."""
    global _installation_token, _installation_token_expires

    # Refresh 5 minutes before expiry so we never use a token right at its edge
    if _installation_token and time.time() < _installation_token_expires - 300:
        return _installation_token

    cfg = _get_config()
    app_jwt = _generate_jwt()

    resp = httpx.post(
        f"https://api.github.com/app/installations/{cfg.GITHUB_APP_INSTALLATION_ID}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    _installation_token = data["token"]
    expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    _installation_token_expires = expires_at.timestamp()

    logger.info(
        "Refreshed GitHub App installation token (expires %s)",
        data["expires_at"],
    )
    return _installation_token


def get_github_token() -> str:
    """Return the active GitHub token — installation token or PAT depending on config."""
    cfg = _get_config()
    if cfg.USING_GITHUB_APP:
        return _get_installation_token()
    return cfg.GITHUB_TOKEN


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_github_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _api_url(path: str) -> str:
    cfg = _get_config()
    return f"https://api.github.com/repos/{cfg.GITHUB_REPO}/{path}"


def create_pr(
    branch: str,
    issue_number: Optional[int],
    summary: str,
    body: str,
    base: str = "main",
) -> str:
    """Create a pull request and return its HTML URL."""
    if issue_number:
        title = f"feat(#{issue_number}): {summary}"
        pr_body = (
            f"Closes #{issue_number}\n\n"
            f"{body}\n\n"
            "---\n"
            "This PR was generated autonomously by Claude Code"
        )
    else:
        title = f"feat: {summary}"
        pr_body = (
            f"{body}\n\n"
            "---\n"
            "This PR was generated autonomously by Claude Code"
        )

    resp = httpx.post(
        _api_url("pulls"),
        headers=_headers(),
        json={
            "title": title,
            "body": pr_body,
            "head": branch,
            "base": base,
        },
        timeout=30,
    )
    resp.raise_for_status()
    pr_url = resp.json()["html_url"]
    logger.info("Created PR: %s", pr_url)
    return pr_url


def comment_on_issue(issue_number: int, body: str) -> None:
    """Post a comment on a GitHub issue."""
    resp = httpx.post(
        _api_url(f"issues/{issue_number}/comments"),
        headers=_headers(),
        json={"body": body},
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("Commented on issue #%d", issue_number)


def add_label(issue_number: int, label: str) -> None:
    """Add a label to an issue."""
    resp = httpx.post(
        _api_url(f"issues/{issue_number}/labels"),
        headers=_headers(),
        json={"labels": [label]},
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("Added label '%s' to issue #%d", label, issue_number)


def add_reaction(issue_number: int, content: str) -> None:
    """Add a reaction to a GitHub issue."""
    resp = httpx.post(
        _api_url(f"issues/{issue_number}/reactions"),
        headers=_headers(),
        json={"content": content},
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("Added reaction '%s' to issue #%d", content, issue_number)


def remove_label(issue_number: int, label: str) -> None:
    """Remove a label from an issue. Ignores 404 (label not present)."""
    resp = httpx.delete(
        _api_url(f"issues/{issue_number}/labels/{label}"),
        headers=_headers(),
        timeout=30,
    )
    if resp.status_code == 404:
        logger.debug("Label '%s' not on issue #%d, skipping", label, issue_number)
        return
    resp.raise_for_status()
    logger.info("Removed label '%s' from issue #%d", label, issue_number)


def reply_to_review_comment(pr_number: int, comment_id: int, body: str) -> None:
    """Post a reply to an inline PR review comment."""
    resp = httpx.post(
        _api_url(f"pulls/{pr_number}/comments"),
        headers=_headers(),
        json={"body": body, "in_reply_to": comment_id},
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("Replied to review comment %d on PR #%d", comment_id, pr_number)


def react_to_review_comment(comment_id: int, content: str) -> None:
    """Add a reaction to an inline PR review comment."""
    resp = httpx.post(
        _api_url(f"pulls/comments/{comment_id}/reactions"),
        headers=_headers(),
        json={"content": content},
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("Reacted '%s' to review comment %d", content, comment_id)


def get_pr_review_comments(pr_number: int, review_id: int) -> list[dict]:
    """Return all inline comments for a specific PR review."""
    resp = httpx.get(
        _api_url(f"pulls/{pr_number}/reviews/{review_id}/comments"),
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    comments = resp.json()
    logger.info("Fetched %d inline comment(s) for PR #%d review %d", len(comments), pr_number, review_id)
    return comments


def get_open_issues_with_label(label: str) -> list[dict]:
    """Return all open issues that have the given label (handles pagination)."""
    issues: list[dict] = []
    page = 1
    while True:
        resp = httpx.get(
            _api_url("issues"),
            headers=_headers(),
            params={"state": "open", "labels": label, "per_page": 100, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        # GitHub issues endpoint also returns pull requests — skip them
        issues.extend(item for item in batch if "pull_request" not in item)
        if len(batch) < 100:
            break
        page += 1
    logger.info("Found %d open issue(s) with label '%s'", len(issues), label)
    return issues
