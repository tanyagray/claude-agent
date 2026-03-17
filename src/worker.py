"""Main worker loop — picks up tasks and invokes Claude Code CLI."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

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


def _ensure_repo() -> None:
    """Clone the repo if it doesn't exist, otherwise fetch latest."""
    if not Path(config.REPO_DIR).exists():
        logger.info("Cloning repo %s", config.GITHUB_REPO)
        subprocess.run(
            [
                "git", "clone",
                f"https://x-access-token:{config.GITHUB_TOKEN}@github.com/{config.GITHUB_REPO}.git",
                config.REPO_DIR,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    else:
        _run_git(["fetch", "origin"])


def _checkout_main() -> None:
    """Checkout and pull the default branch."""
    _run_git(["checkout", "main"])
    _run_git(["pull", "origin", "main"])


def _create_branch(task: Task) -> str:
    """Create a working branch for the task."""
    ts = int(time.time())
    if task.issue_number:
        slug = _slugify(task.summary)[:30]
        branch = f"claude/issue-{task.issue_number}-{slug}-{ts}"
    else:
        branch = f"claude/docs-update-{ts}"

    _run_git(["checkout", "-b", branch])
    return branch


def _has_changes() -> bool:
    """Check if there are uncommitted changes in the working tree."""
    result = _run_git(["status", "--porcelain"])
    return bool(result.stdout.strip())


def _cleanup_branch(branch: str) -> None:
    """Switch back to main and delete the working branch."""
    _run_git(["checkout", "main"], check=False)
    _run_git(["branch", "-D", branch], check=False)


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
- If you genuinely cannot complete the task, create a file called CLAUDE_BLOCKED.md explaining what you need and why you're stuck""")

    return "\n".join(parts)


def _run_claude(task: Task) -> subprocess.CompletedProcess[str]:
    """Invoke the Claude Code CLI."""
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

    logger.info("Running Claude Code CLI (timeout=%ds)", config.CLAUDE_TIMEOUT)
    logger.info("Prompt length: %d chars", len(prompt))

    proc = subprocess.Popen(
        cmd,
        cwd=config.REPO_DIR,
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


def process_task(task: Task, task_queue: TaskQueue) -> None:
    """Process a single task end-to-end."""
    branch = ""
    try:
        # 0. React with heart emoji to signal we've picked up the issue
        if task.issue_number:
            github_api.add_reaction(task.issue_number, "heart")

        # 1. Ensure repo is cloned and up to date
        _ensure_repo()
        _checkout_main()

        # 2. Create working branch
        branch = _create_branch(task)
        logger.info("Working on branch: %s", branch)

        # 3. Run Claude Code
        result = _run_claude(task)
        if result.returncode != 0:
            logger.warning("Claude Code exited with code %d", result.returncode)
            logger.warning("stderr: %s", result.stderr[-2000:] if result.stderr else "(empty)")

        # 4. Check for blocked state
        blocked_file = Path(config.REPO_DIR) / "CLAUDE_BLOCKED.md"
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
            _cleanup_branch(branch)
            return

        # 5. Check for changes
        if _has_changes():
            commit_msg = f"feat(#{task.issue_number}): {task.summary}" if task.issue_number else f"feat: {task.summary}"
            _run_git(["add", "-A"])
            _run_git(["commit", "-m", commit_msg])
            _run_git(["push", "origin", branch])

            pr_url = github_api.create_pr(
                branch=branch,
                issue_number=task.issue_number,
                summary=task.summary,
                body=task.body,
            )

            if task.issue_number:
                github_api.comment_on_issue(task.issue_number, f"PR ready: {pr_url}")
                github_api.add_label(task.issue_number, "claude-pr-open")
                github_api.remove_label(task.issue_number, config.TRIGGER_LABEL)

            task_queue.complete_task(task.id, branch_name=branch, pr_url=pr_url)
            notify.send(f"PR opened for #{task.issue_number}: {task.summary}\n{pr_url}")
            _cleanup_branch(branch)
        else:
            # No changes produced
            will_retry = task_queue.fail_task(task.id, "No changes produced")
            if will_retry:
                notify.send(
                    f"No changes for #{task.issue_number}, retrying "
                    f"({task.retries + 1}/{task.max_retries})"
                )
            else:
                if task.issue_number:
                    github_api.comment_on_issue(
                        task.issue_number,
                        "Couldn't determine changes needed after multiple attempts. Needs human input.",
                    )
                notify.send(f"Failed #{task.issue_number}: {task.summary}")
            _cleanup_branch(branch)

    except subprocess.TimeoutExpired:
        logger.error("Claude Code timed out for task %s", task.id)
        task_queue.fail_task(task.id, "Claude Code timed out")
        notify.send(f"Timeout on #{task.issue_number}: {task.summary}")
        if branch:
            _cleanup_branch(branch)

    except Exception as e:
        logger.exception("Error processing task %s", task.id)
        task_queue.fail_task(task.id, str(e))
        notify.send(f"Error on #{task.issue_number}: {e}")
        if branch:
            _cleanup_branch(branch)


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
