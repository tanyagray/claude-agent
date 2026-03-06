"""GitHub REST API helpers for PRs, issues, and labels."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Lazy import to avoid circular dependency at module level
_config = None


def _get_config():
    global _config
    if _config is None:
        from src import config
        _config = config
    return _config


def _headers() -> dict[str, str]:
    cfg = _get_config()
    return {
        "Authorization": f"Bearer {cfg.GITHUB_TOKEN}",
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
