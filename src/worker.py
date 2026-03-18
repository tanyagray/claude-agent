"""Main worker loop — picks up tasks and invokes Claude Code CLI."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

import httpx

from src import config
from src import github_api
from src import notify
from src.tasks import Task, TaskQueue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def _run_git(args: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command."""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd or config.REPO_DIR,
        capture_output=True,
        text=True,
        check=check,
        timeout=120,
    )


def _refresh_remote_url() -> None:
    """Update the origin remote URL with a fresh token.

    GitHub App installation tokens expire after 1 hour. Without this,
    git fetch/push would fail with a 401 once the token embedded in
    the clone URL expires.
    """
    token = github_api.get_github_token()
    _run_git([
        "remote", "set-url", "origin",
        f"https://x-access-token:{token}@github.com/{config.GITHUB_REPO}.git",
    ])


def _ensure_repo() -> None:
    """Clone the repo if it doesn't exist, otherwise fetch latest."""
    if not Path(config.REPO_DIR).exists():
        logger.info("Cloning repo %s", config.GITHUB_REPO)
        token = github_api.get_github_token()
        subprocess.run(
            [
                "git", "clone",
                f"https://x-access-token:{token}@github.com/{config.GITHUB_REPO}.git",
                config.REPO_DIR,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    else:
        _refresh_remote_url()
        _run_git(["fetch", "origin"])


def _checkout_main() -> None:
    """Checkout and pull the default branch in the main repo clone."""
    _run_git(["checkout", "main"])
    _run_git(["pull", "origin", "main"])


def _create_worktree(task: Task) -> tuple[str, str]:
    """Create an isolated git worktree for the task.

    Returns (branch_name, worktree_path).
    The branch is created from the current main HEAD in the main repo clone.
    """
    ts = int(time.time())
    if task.issue_number:
        slug = _slugify(task.summary)[:30]
        branch = f"claude/issue-{task.issue_number}-{slug}-{ts}"
    else:
        branch = f"claude/docs-update-{ts}"

    worktree_path = str(Path(config.WORKTREES_DIR) / branch)
    Path(config.WORKTREES_DIR).mkdir(parents=True, exist_ok=True)

    # -b creates the branch from the current HEAD (main) and checks it out in
    # an isolated directory — completely separate from the main repo clone.
    _run_git(["worktree", "add", "-b", branch, worktree_path, "main"])
    logger.info("Created worktree %s on branch %s", worktree_path, branch)
    return branch, worktree_path


def _has_uncommitted_changes(cwd: str) -> bool:
    """Check if there are uncommitted changes in the given working tree."""
    result = _run_git(["status", "--porcelain"], cwd=cwd)
    return bool(result.stdout.strip())


def _has_branch_commits(cwd: str) -> bool:
    """Check if there are commits on the current branch beyond main."""
    result = _run_git(["log", "main..HEAD", "--oneline"], cwd=cwd, check=False)
    return bool(result.stdout.strip())


def _cleanup_worktree(branch: str, worktree_path: str) -> None:
    """Remove the worktree directory and delete the local branch."""
    _run_git(["worktree", "remove", "--force", worktree_path], check=False)
    _run_git(["branch", "-D", branch], check=False)
    logger.info("Cleaned up worktree %s (branch %s)", worktree_path, branch)


def _create_review_worktree(branch: str) -> str:
    """Check out an existing PR branch into an isolated worktree (detached HEAD).

    Returns the worktree_path.
    """
    worktree_path = str(Path(config.WORKTREES_DIR) / branch)
    Path(config.WORKTREES_DIR).mkdir(parents=True, exist_ok=True)
    _run_git(["worktree", "add", "--detach", worktree_path, f"origin/{branch}"])
    logger.info("Created review worktree %s for branch %s", worktree_path, branch)
    return worktree_path


def _cleanup_review_worktree(worktree_path: str) -> None:
    """Remove a review worktree (detached — no local branch to delete)."""
    _run_git(["worktree", "remove", "--force", worktree_path], check=False)
    logger.info("Cleaned up review worktree %s", worktree_path)


def _build_error_comment(error: Exception) -> str:
    """Build a user-friendly GitHub comment explaining an error to the repo owner."""
    lines = ["⚠️ **Claude Agent encountered an error working on this issue.**\n"]

    if isinstance(error, subprocess.TimeoutExpired):
        lines.append(f"**Error:** Claude Code timed out after {config.CLAUDE_TIMEOUT} seconds.\n")
        lines.append("**Possible causes:**")
        lines.append("- The task may be too complex to complete within the time limit")
        lines.append(f"- Current timeout: `CLAUDE_TIMEOUT={config.CLAUDE_TIMEOUT}` — increase this in your agent config if needed\n")

    elif isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        url = str(error.request.url)
        lines.append(f"**Error:** GitHub API request failed with HTTP {status}.\n")
        if status in (401, 403):
            lines.append("**Possible causes:**")
            lines.append("- `GITHUB_TOKEN` is missing, expired, or lacks required permissions")
            lines.append("- The token needs `repo` scope (or `public_repo` for public repos)")
            lines.append("- For creating PRs and comments, ensure the token has write access to the repository\n")
        elif status == 404:
            lines.append("**Possible causes:**")
            lines.append(f"- Resource not found: `{url}`")
            lines.append("- `GITHUB_REPO` may be set incorrectly (expected format: `owner/repo`)")
            lines.append("- The token may not have access to this repository\n")
        elif status == 422:
            lines.append("**Possible causes:**")
            lines.append("- A PR for this branch may already exist")
            lines.append("- The branch may have no commits relative to the base branch\n")
        elif status == 429:
            lines.append("**Possible causes:**")
            lines.append("- GitHub API rate limit exceeded")
            lines.append("- The agent will retry automatically\n")
        elif status >= 500:
            lines.append("**Possible causes:**")
            lines.append("- GitHub may be experiencing an outage — check https://githubstatus.com")
            lines.append("- The agent will retry automatically\n")

    elif isinstance(error, httpx.ConnectError | httpx.TimeoutException):
        lines.append("**Error:** Could not connect to the GitHub API.\n")
        lines.append("**Possible causes:**")
        lines.append("- Network connectivity issue from the agent host")
        lines.append("- GitHub may be experiencing an outage — check https://githubstatus.com\n")

    elif isinstance(error, subprocess.CalledProcessError):
        cmd = " ".join(str(a) for a in error.cmd) if error.cmd else "unknown"
        lines.append(f"**Error:** A git/shell command failed (exit code {error.returncode}).\n")
        lines.append("**Possible causes:**")
        lines.append("- `GITHUB_TOKEN` may not have push access to the repository")
        lines.append("- The repository clone may be in a bad state")
        if error.stderr:
            lines.append(f"\n**Details:**\n```\n{error.stderr[:500]}\n```")

    else:
        lines.append(f"**Error:** `{type(error).__name__}: {str(error)[:300]}`\n")
        lines.append("**Possible causes:**")
        lines.append("- A required environment variable may be misconfigured")
        lines.append("- Check agent logs for more details\n")

    lines.append("---")
    lines.append("*You can re-trigger by commenting `/claude` on this issue once the issue is resolved.*")
    return "\n".join(lines)


def build_prompt(task: Task) -> str:
    """Build the Claude Code prompt for a task."""
    parts = [
        "You are an autonomous developer working on a side project.\n",
        "## Task",
    ]

    if task.issue_number:
        parts.append(f"Issue #{task.issue_number}: {task.summary}\n")
    else:
        parts.append(f"{task.summary}\n")

    parts.append("## Description")
    parts.append(task.body + "\n")

    if task.additional_context:
        parts.append("## Additional Instructions")
        parts.append(task.additional_context + "\n")

    parts.append("""## Progress Reporting
Before each major step, run: echo '[PROGRESS] <description>'
For example:
  echo '[PROGRESS] Reading issue details and codebase...'
  echo '[PROGRESS] Implementing the feature...'
  echo '[PROGRESS] Running tests...'
  echo '[PROGRESS] Done.'
For any step that might take more than 30 seconds (API calls, file processing, long test runs), \
print additional [PROGRESS] updates every ~30s so the log stays active.

## Project Context
Read all files in ./docs/ for project context, architecture, and coding standards.
Also read the README.md in the project root for an overview.

## Instructions
1. Read the existing codebase and documentation before writing any code
2. Implement the feature or fix described above
3. Follow existing code patterns and conventions in the repo
4. Write or update tests if the project has a test suite
5. Run any existing test/lint/build commands you find (package.json scripts, Makefile, etc.)
6. Fix any test or lint failures before finishing
7. Do NOT ask for clarification — make reasonable decisions and note any assumptions in your commit message

## Constraints
- Only modify files relevant to this task
- Do not refactor unrelated code
- Keep changes focused and reviewable
- **Do NOT run any git commands** (no git add, git commit, git push, git checkout, git branch, etc.) — the system will handle all version control after you finish
- If you genuinely cannot complete the task, create a file called CLAUDE_BLOCKED.md explaining what you need and why you're stuck""")

    return "\n".join(parts)


def build_review_prompt(task: Task) -> str:
    """Build the Claude Code prompt for addressing PR review feedback."""
    parts = [
        "You are an autonomous developer working on a side project.\n",
        f"## Task\nAddress code review feedback on PR #{task.pr_number}\n",
        "## Review Feedback",
        task.body + "\n",
    ]

    if task.additional_context:
        parts.append("## Additional Instructions")
        parts.append(task.additional_context + "\n")

    parts.append("""## Progress Reporting
Before each major step, run: echo '[PROGRESS] <description>'

## Project Context
Read all files in ./docs/ for project context, architecture, and coding standards.
Also read the README.md in the project root for an overview.

## Instructions
1. Read the current state of the code on this branch before making any changes
2. Address each piece of review feedback carefully
3. For inline comments, find the exact file and line referenced and fix the issue
4. Follow existing code patterns and conventions in the repo
5. Run any existing test/lint/build commands you find (package.json scripts, Makefile, etc.)
6. Fix any test or lint failures before finishing
7. Do NOT ask for clarification — make reasonable decisions based on the reviewer's intent
8. After making all code changes, create a file called CLAUDE_REVIEW_RESPONSES.json in the repo root

## CLAUDE_REVIEW_RESPONSES.json format
For each inline comment (identified by its comment_id), decide:
- If you simply did what was asked with no caveats: use a thumbs-up reaction, no text reply
- If you took a different approach, made a tradeoff, or want to explain your reasoning: write a short text reply

Write the file as a JSON array, one entry per inline comment:
```json
[
  {"comment_id": 123, "reaction": "+1", "reply": null},
  {"comment_id": 456, "reaction": null, "reply": "I went with X instead because Y — happy to change if you disagree."}
]
```
Only include comments that appeared in the review feedback above (use the comment_id values listed there).
Do not include this file in the git commit — it will be read and then deleted by the automation.

## Constraints
- Only modify files relevant to the review feedback
- Do not refactor unrelated code
- Keep changes focused and reviewable
- **Do NOT run any git commands** — the system will handle all version control after you finish
- If you genuinely cannot address the feedback, create a file called CLAUDE_BLOCKED.md explaining what you need and why you're stuck""")

    return "\n".join(parts)


def _parse_claude_error(stderr: str) -> str | None:
    """Extract a human-readable error from Claude CLI stderr, or None if unrecognised."""
    if not stderr:
        return None
    low = stderr.lower()
    if any(p in low for p in ("out of credit", "insufficient credit", "credit balance", "no credits")):
        return "Claude is out of API credits — please top up your Anthropic account balance."
    if any(p in low for p in ("claude max", "max subscription", "subscription required")):
        return (
            "Claude Max subscription error. "
            "CLAUDE_USE_MAX is only valid for local development with a personal Claude Max plan. "
            "On a server, set ANTHROPIC_API_KEY instead."
        )
    if any(p in low for p in ("invalid api key", "authentication", "unauthorized", "401")):
        return "Claude authentication failed — check that ANTHROPIC_API_KEY is valid."
    if any(p in low for p in ("rate limit", "too many requests", "429")):
        return "Claude hit a rate limit."
    if any(p in low for p in ("quota", "usage limit", "usage_limit")):
        return "Claude usage quota exceeded."
    # Fall back to the last non-empty stderr line
    lines = [ln.strip() for ln in stderr.strip().splitlines() if ln.strip()]
    if lines:
        return f"Claude error: {lines[-1]}"
    return None


_NON_RETRYABLE_ERRORS = (
    "out of api credits",
    "top up",
    "authentication failed",
    "invalid api key",
    "claude max subscription error",
)


def _is_retryable(error_msg: str) -> bool:
    low = error_msg.lower()
    return not any(phrase in low for phrase in _NON_RETRYABLE_ERRORS)


def _run_claude(task: Task, cwd: str) -> subprocess.CompletedProcess[str]:
    """Invoke the Claude Code CLI in the given working directory (worktree)."""
    if task.event_type == "pr_review_changes_requested":
        prompt = build_review_prompt(task)
    else:
        prompt = build_prompt(task)

    cmd = [
        "claude",
        "-p",
        "--allowedTools", "Bash,Read,Write,Edit",
        "--dangerously-skip-permissions",
    ]
    if config.CLAUDE_USE_MAX:
        cmd.append("--max")

    env = {**os.environ}
    if config.ANTHROPIC_API_KEY:
        env["ANTHROPIC_API_KEY"] = config.ANTHROPIC_API_KEY

    logger.info("Running Claude Code CLI in %s (timeout=%ds)", cwd, config.CLAUDE_TIMEOUT)
    logger.info("Prompt length: %d chars", len(prompt))

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    proc.stdin.write(prompt)
    proc.stdin.close()

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    import selectors
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    sel.register(proc.stderr, selectors.EVENT_READ)

    open_streams = 2
    start_time = time.time()
    while open_streams > 0:
        elapsed = time.time() - start_time
        if elapsed > config.CLAUDE_TIMEOUT:
            proc.kill()
            raise subprocess.TimeoutExpired(cmd, config.CLAUDE_TIMEOUT)

        remaining = config.CLAUDE_TIMEOUT - elapsed
        events = sel.select(timeout=min(remaining, 30))

        if not events:
            logger.info("Claude Code still running... (%.0fs elapsed)", elapsed)
            continue

        for key, _ in events:
            line = key.fileobj.readline()
            if not line:
                sel.unregister(key.fileobj)
                open_streams -= 1
                continue
            line = line.rstrip("\n")
            if key.fileobj is proc.stdout:
                stdout_lines.append(line)
                logger.info("claude> %s", line[:500])
            else:
                stderr_lines.append(line)
                logger.warning("claude-err> %s", line[:500])

    sel.close()
    proc.wait()

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode,
        stdout="\n".join(stdout_lines),
        stderr="\n".join(stderr_lines),
    )


def _post_review_responses(pr_number: int, responses: list[dict]) -> None:
    """Post per-comment reactions and replies from CLAUDE_REVIEW_RESPONSES.json."""
    for entry in responses:
        comment_id = entry.get("comment_id")
        if not comment_id:
            continue
        reaction = entry.get("reaction")
        reply = entry.get("reply")
        try:
            if reaction:
                github_api.react_to_review_comment(comment_id, reaction)
            if reply:
                github_api.reply_to_review_comment(pr_number, comment_id, reply)
        except Exception:
            logger.exception("Failed to post response for comment %d", comment_id)


def _process_pr_review_task(task: Task, task_queue: TaskQueue) -> None:
    """Process a PR review 'changes_requested' task using an isolated worktree."""
    branch = task.branch_name or ""
    worktree_path = ""
    if not branch:
        logger.error("PR review task %s has no branch_name, cannot process", task.id)
        task_queue.fail_task(task.id, "No branch_name set on PR review task", force_no_retry=True)
        return

    try:
        # 1. Ensure repo is up to date and check out the PR branch in a worktree
        _ensure_repo()
        worktree_path = _create_review_worktree(branch)
        logger.info("Working on PR #%d review in worktree %s", task.pr_number, worktree_path)

        # 2. Run Claude Code with the review feedback prompt
        result = _run_claude(task, cwd=worktree_path)
        if result.returncode != 0:
            logger.warning("Claude Code exited with code %d", result.returncode)
            logger.warning("stderr: %s", result.stderr[-2000:] if result.stderr else "(empty)")
            error_msg = _parse_claude_error(result.stderr)
            if error_msg and not _is_retryable(error_msg):
                github_api.comment_on_issue(
                    task.pr_number,
                    f"Claude encountered an error and could not address the review:\n\n> {error_msg}",
                )
                task_queue.fail_task(task.id, error_msg, force_no_retry=True)
                notify.send(f"Fatal error on PR #{task.pr_number} review: {error_msg}")
                _cleanup_review_worktree(worktree_path)
                return

        # 3. Check for blocked state
        blocked_file = Path(worktree_path) / "CLAUDE_BLOCKED.md"
        if blocked_file.exists():
            blocked_reason = blocked_file.read_text(encoding="utf-8")
            blocked_file.unlink()
            logger.warning("Claude is blocked: %s", blocked_reason[:200])

            github_api.comment_on_issue(
                task.pr_number,
                f"I got stuck trying to address the review feedback:\n\n{blocked_reason}",
            )
            task_queue.fail_task(task.id, blocked_reason)
            notify.send(f"Blocked addressing review on PR #{task.pr_number}: {task.summary}")
            _cleanup_review_worktree(worktree_path)
            return

        # 4. Read and remove the review responses file before committing
        import json as _json
        responses_file = Path(worktree_path) / "CLAUDE_REVIEW_RESPONSES.json"
        review_responses: list[dict] = []
        if responses_file.exists():
            try:
                review_responses = _json.loads(responses_file.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("Failed to parse CLAUDE_REVIEW_RESPONSES.json")
            responses_file.unlink()

        # 5. Check for changes and commit/push
        if _has_uncommitted_changes(worktree_path):
            _run_git(["add", "-A"], cwd=worktree_path)
            _run_git(["commit", "-m", f"fix: address review feedback on PR #{task.pr_number}"], cwd=worktree_path)
            # Push detached HEAD back to the PR branch on origin
            _refresh_remote_url()  # token may have expired during long Claude run
            _run_git(["push", "origin", f"HEAD:{branch}"], cwd=worktree_path)

            _post_review_responses(task.pr_number, review_responses)

            github_api.comment_on_issue(
                task.pr_number,
                "I've addressed the review feedback. Please take another look.",
            )
            task_queue.complete_task(task.id, branch_name=branch, pr_url=task.pr_url)
            notify.send(f"Addressed review on PR #{task.pr_number}: {task.summary}")
        else:
            claude_error = _parse_claude_error(result.stderr) if result.returncode != 0 else None
            will_retry = task_queue.fail_task(task.id, claude_error or "No changes produced from review feedback")
            if will_retry:
                notify.send(
                    f"No changes for PR #{task.pr_number} review, retrying "
                    f"({task.retries + 1}/{task.max_retries})"
                )
            else:
                github_api.comment_on_issue(
                    task.pr_number,
                    "I wasn't able to determine how to address the review feedback after multiple attempts. Needs human input.",
                )
                notify.send(f"Failed to address review on PR #{task.pr_number}: {task.summary}")

        _cleanup_review_worktree(worktree_path)

    except subprocess.TimeoutExpired as e:
        logger.error("Claude Code timed out for task %s", task.id)
        task_queue.fail_task(task.id, "Claude Code timed out")
        notify.send(f"Timeout on PR #{task.pr_number} review: {task.summary}")
        try:
            github_api.comment_on_issue(task.pr_number, _build_error_comment(e))
        except Exception:
            logger.exception("Failed to post timeout error comment to GitHub")
        if worktree_path:
            _cleanup_review_worktree(worktree_path)

    except Exception as e:
        logger.exception("Error processing PR review task %s", task.id)
        task_queue.fail_task(task.id, str(e))
        notify.send(f"Error on PR #{task.pr_number} review: {e}")
        try:
            github_api.comment_on_issue(task.pr_number, _build_error_comment(e))
        except Exception:
            logger.exception("Failed to post error comment to GitHub")
        if worktree_path:
            _cleanup_review_worktree(worktree_path)


def process_task(task: Task, task_queue: TaskQueue) -> None:
    """Process a single task end-to-end."""
    if task.event_type == "pr_review_changes_requested":
        _process_pr_review_task(task, task_queue)
        return

    branch = ""
    worktree_path = ""
    try:
        # 0. React with heart emoji to signal we've picked up the issue
        if task.issue_number:
            github_api.add_reaction(task.issue_number, "heart")

        # 1. Ensure repo is cloned and up to date on main
        _ensure_repo()
        _checkout_main()

        # 2. Create an isolated worktree branched from main
        branch, worktree_path = _create_worktree(task)
        logger.info("Working on branch %s in worktree %s", branch, worktree_path)

        # 3. Run Claude Code inside the isolated worktree
        result = _run_claude(task, cwd=worktree_path)
        if result.returncode != 0:
            logger.warning("Claude Code exited with code %d", result.returncode)
            logger.warning("stderr: %s", result.stderr[-2000:] if result.stderr else "(empty)")
            error_msg = _parse_claude_error(result.stderr)
            if error_msg and not _is_retryable(error_msg):
                # Fatal error — surface it immediately, no point retrying
                logger.error("Non-retryable Claude error: %s", error_msg)
                if task.issue_number:
                    github_api.comment_on_issue(
                        task.issue_number,
                        f"Claude encountered an error and could not complete this task:\n\n> {error_msg}",
                    )
                task_queue.fail_task(task.id, error_msg, force_no_retry=True)
                notify.send(f"Fatal error on #{task.issue_number}: {error_msg}")
                _cleanup_worktree(branch, worktree_path)
                return

        # 4. Check for blocked state
        blocked_file = Path(worktree_path) / "CLAUDE_BLOCKED.md"
        if blocked_file.exists():
            blocked_reason = blocked_file.read_text(encoding="utf-8")
            blocked_file.unlink()
            logger.warning("Claude is blocked: %s", blocked_reason[:200])

            if task.issue_number:
                github_api.comment_on_issue(
                    task.issue_number,
                    f"I got stuck:\n\n{blocked_reason}",
                )
                github_api.add_label(task.issue_number, "claude-blocked")

            task_queue.fail_task(task.id, blocked_reason)
            notify.send(f"Blocked on #{task.issue_number}: {task.summary}")
            _cleanup_worktree(branch, worktree_path)
            return

        # 5. Check for changes in the worktree (uncommitted or already committed by Claude)
        has_uncommitted = _has_uncommitted_changes(worktree_path)
        has_commits = _has_branch_commits(worktree_path)

        if has_uncommitted or has_commits:
            if has_uncommitted:
                commit_msg = f"feat(#{task.issue_number}): {task.summary}" if task.issue_number else f"feat: {task.summary}"
                _run_git(["add", "-A"], cwd=worktree_path)
                _run_git(["commit", "-m", commit_msg], cwd=worktree_path)
            else:
                logger.info("Claude committed changes directly on branch %s", branch)

            _refresh_remote_url()  # token may have expired during long Claude run
            _run_git(["push", "origin", branch], cwd=worktree_path)

            pr_url = github_api.create_pr(
                branch=branch,
                issue_number=task.issue_number,
                summary=task.summary,
                body=task.body,
            )

            if task.issue_number and pr_url:
                github_api.comment_on_issue(task.issue_number, f"PR ready: {pr_url}")
                github_api.add_label(task.issue_number, "claude-pr-open")
                github_api.remove_label(task.issue_number, config.TRIGGER_LABEL)

            task_queue.complete_task(task.id, branch_name=branch, pr_url=pr_url)
            notify.send(f"PR opened for #{task.issue_number}: {task.summary}\n{pr_url}")
            _cleanup_worktree(branch, worktree_path)
        else:
            # No changes produced
            claude_error = _parse_claude_error(result.stderr) if result.returncode != 0 else None
            will_retry = task_queue.fail_task(task.id, claude_error or "No changes produced")
            if will_retry:
                notify.send(
                    f"No changes for #{task.issue_number}, retrying "
                    f"({task.retries + 1}/{task.max_retries})"
                )
            else:
                if task.issue_number:
                    if claude_error:
                        comment = (
                            f"Claude encountered an error and could not complete this task:\n\n> {claude_error}"
                        )
                    else:
                        comment = "Couldn't determine changes needed after multiple attempts. Needs human input."
                    github_api.comment_on_issue(task.issue_number, comment)
                notify.send(f"Failed #{task.issue_number}: {claude_error or task.summary}")
            _cleanup_worktree(branch, worktree_path)

    except subprocess.TimeoutExpired as e:
        logger.error("Claude Code timed out for task %s", task.id)
        task_queue.fail_task(task.id, "Claude Code timed out")
        notify.send(f"Timeout on #{task.issue_number}: {task.summary}")
        if task.issue_number:
            try:
                github_api.comment_on_issue(task.issue_number, _build_error_comment(e))
            except Exception:
                logger.exception("Failed to post timeout error comment to GitHub")
        if worktree_path:
            _cleanup_worktree(branch, worktree_path)

    except Exception as e:
        logger.exception("Error processing task %s", task.id)
        task_queue.fail_task(task.id, str(e))
        notify.send(f"Error on #{task.issue_number}: {e}")
        if task.issue_number:
            try:
                github_api.comment_on_issue(task.issue_number, _build_error_comment(e))
            except Exception:
                logger.exception("Failed to post error comment to GitHub")
        if worktree_path:
            _cleanup_worktree(branch, worktree_path)


def _sync_open_issues(task_queue: TaskQueue) -> None:
    """On startup, queue any open issues with the trigger label not already tracked."""
    logger.info("Checking GitHub for open issues with label '%s'", config.TRIGGER_LABEL)
    try:
        issues = github_api.get_open_issues_with_label(config.TRIGGER_LABEL)
    except Exception:
        logger.exception("Failed to fetch open issues from GitHub; skipping startup sync")
        return

    # Build a set of issue numbers already in the active queue
    tracked: set[int] = set()
    for status in ("pending", "in_progress"):
        for task in task_queue.list_tasks(status):
            if task.issue_number is not None:
                tracked.add(task.issue_number)

    queued = 0
    for issue in issues:
        issue_number: int = issue["number"]
        if issue_number in tracked:
            logger.debug("Issue #%d already in queue, skipping", issue_number)
            continue

        # Skip issues that the agent already handled (PR open or blocked)
        label_names = {lbl["name"] for lbl in issue.get("labels", [])}
        if "claude-pr-open" in label_names or "claude-blocked" in label_names:
            logger.debug("Issue #%d already processed (labels: %s), skipping", issue_number, label_names)
            continue

        task_queue.create_task(
            source="github_issue",
            event_type="issue_labeled",
            summary=issue["title"],
            body=issue.get("body") or "",
            issue_number=issue_number,
            max_retries=config.MAX_RETRIES_PER_TASK,
        )
        logger.info("Queued missed issue #%d on startup: %s", issue_number, issue["title"])
        queued += 1

    logger.info("Startup sync complete: %d issue(s) added to queue", queued)


def main() -> None:
    """Main worker loop — runs forever, picks up one task at a time."""
    logger.info("Worker started (poll_interval=%ds)", config.POLL_INTERVAL)
    task_queue = TaskQueue(config.TASKS_DIR)

    _sync_open_issues(task_queue)

    while True:
        task = task_queue.get_next_task()
        if not task:
            time.sleep(config.POLL_INTERVAL)
            continue

        logger.info("Processing task: %s — %s", task.id, task.summary)
        process_task(task, task_queue)
        logger.info("Finished task: %s", task.id)


if __name__ == "__main__":
    main()
