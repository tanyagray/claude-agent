"""File-based task queue manager.

Tasks are markdown files with YAML frontmatter stored in status subdirectories:
  /data/tasks/pending/
  /data/tasks/in_progress/
  /data/tasks/completed/
  /data/tasks/failed/
"""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import frontmatter

logger = logging.getLogger(__name__)

STATUSES = ("pending", "in_progress", "completed", "failed")


@dataclass
class Task:
    id: str
    source: str
    event_type: str
    summary: str
    body: str
    issue_number: Optional[int] = None
    priority: int = 0
    retries: int = 0
    max_retries: int = 3
    branch_name: Optional[str] = None
    pr_url: Optional[str] = None
    error_log: Optional[str] = None
    created_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    additional_context: Optional[str] = None
    # runtime field — which folder it's currently in
    status: str = "pending"


class TaskQueue:
    """Manages task files across status directories."""

    def __init__(self, tasks_dir: str) -> None:
        self.base = Path(tasks_dir)
        self._ensure_dirs()
        self._lock_path = self.base / ".queue.lock"

    def _ensure_dirs(self) -> None:
        for status in STATUSES:
            (self.base / status).mkdir(parents=True, exist_ok=True)

    def _lock(self) -> int:
        """Acquire an exclusive file lock."""
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _unlock(self, fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    # --- Public API ---

    def create_task(
        self,
        source: str,
        event_type: str,
        summary: str,
        body: str,
        issue_number: Optional[int] = None,
        additional_context: Optional[str] = None,
        max_retries: int = 3,
    ) -> Task:
        """Create a new task file in pending/."""
        now = datetime.now(timezone.utc)
        ts = int(now.timestamp())
        slug = issue_number or _slugify(summary)[:30]
        task_id = f"{ts}-{source}-{slug}"
        filename = f"{task_id}.md"

        task = Task(
            id=task_id,
            source=source,
            event_type=event_type,
            summary=summary,
            body=body,
            issue_number=issue_number,
            max_retries=max_retries,
            created_at=now.isoformat(),
            additional_context=additional_context,
        )

        fd = self._lock()
        try:
            path = self.base / "pending" / filename
            _write_task(path, task)
            logger.info("Created task %s", task_id)
        finally:
            self._unlock(fd)

        return task

    def get_next_task(self) -> Optional[Task]:
        """Pop the oldest pending task and move it to in_progress/."""
        fd = self._lock()
        try:
            pending_dir = self.base / "pending"
            files = sorted(pending_dir.glob("*.md"))
            if not files:
                return None

            src_path = files[0]
            task = _read_task(src_path, "pending")

            # Update started_at
            task.started_at = datetime.now(timezone.utc).isoformat()
            task.status = "in_progress"

            dst_path = self.base / "in_progress" / src_path.name
            _write_task(dst_path, task)
            src_path.unlink()

            logger.info("Picked up task %s", task.id)
            return task
        finally:
            self._unlock(fd)

    def complete_task(
        self,
        task_id: str,
        branch_name: Optional[str] = None,
        pr_url: Optional[str] = None,
    ) -> None:
        """Move a task from in_progress/ to completed/."""
        fd = self._lock()
        try:
            src_path = self._find_task_file(task_id, "in_progress")
            if not src_path:
                logger.warning("Cannot complete task %s — not found in in_progress", task_id)
                return

            task = _read_task(src_path, "in_progress")
            task.branch_name = branch_name
            task.pr_url = pr_url
            task.completed_at = datetime.now(timezone.utc).isoformat()
            task.status = "completed"

            dst_path = self.base / "completed" / src_path.name
            _write_task(dst_path, task)
            src_path.unlink()

            logger.info("Completed task %s", task_id)
        finally:
            self._unlock(fd)

    def fail_task(self, task_id: str, error: str, force_no_retry: bool = False) -> bool:
        """Fail a task. Returns True if it will retry, False if permanently failed."""
        fd = self._lock()
        try:
            src_path = self._find_task_file(task_id, "in_progress")
            if not src_path:
                logger.warning("Cannot fail task %s — not found in in_progress", task_id)
                return False

            task = _read_task(src_path, "in_progress")
            task.retries += 1
            task.error_log = error
            task.started_at = None

            will_retry = not force_no_retry and task.retries < task.max_retries
            if will_retry:
                task.status = "pending"
                # Use current timestamp in filename so retried tasks go to the
                # end of the queue instead of always being picked up first.
                now_ts = int(datetime.now(timezone.utc).timestamp())
                retry_name = f"{now_ts}-{task.source}-{task.issue_number or task.id.split('-', 1)[-1]}.md"
                dst_path = self.base / "pending" / retry_name
                logger.info("Task %s failed (attempt %d/%d), retrying", task_id, task.retries, task.max_retries)
            else:
                task.status = "failed"
                task.completed_at = datetime.now(timezone.utc).isoformat()
                dst_path = self.base / "failed" / src_path.name
                logger.info("Task %s permanently failed after %d attempts", task_id, task.retries)

            _write_task(dst_path, task)
            src_path.unlink()

            return will_retry
        finally:
            self._unlock(fd)

    def list_tasks(self, status: str) -> list[Task]:
        """List all tasks with a given status."""
        folder = self.base / status
        if not folder.exists():
            return []
        tasks = []
        for path in sorted(folder.glob("*.md")):
            try:
                tasks.append(_read_task(path, status))
            except Exception:
                logger.exception("Failed to read task file %s", path)
        return tasks

    def get_task(self, task_id: str) -> Optional[Task]:
        """Find a task by ID across all status folders."""
        for status in STATUSES:
            path = self._find_task_file(task_id, status)
            if path:
                return _read_task(path, status)
        return None

    def _find_task_file(self, task_id: str, status: str) -> Optional[Path]:
        """Find a task file by ID in a specific status folder."""
        folder = self.base / status
        for path in folder.glob("*.md"):
            if path.stem == task_id:
                return path
        return None


# --- File I/O helpers ---


def _write_task(path: Path, task: Task) -> None:
    """Write a task to a markdown file with YAML frontmatter."""
    metadata = {
        "id": task.id,
        "source": task.source,
        "event_type": task.event_type,
        "issue_number": task.issue_number,
        "summary": task.summary,
        "priority": task.priority,
        "retries": task.retries,
        "max_retries": task.max_retries,
        "branch_name": task.branch_name,
        "pr_url": task.pr_url,
        "error_log": task.error_log,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
    }
    content_parts = ["## Issue Body\n", task.body or "(no body)"]
    if task.additional_context:
        content_parts.append("\n\n## Additional Context\n")
        content_parts.append(task.additional_context)

    post = frontmatter.Post("\n".join(content_parts), **metadata)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _read_task(path: Path, status: str) -> Task:
    """Parse a task markdown file."""
    post = frontmatter.load(str(path))
    meta = post.metadata

    # Extract additional context from content if present
    content = post.content
    additional_context = None
    if "## Additional Context" in content:
        parts = content.split("## Additional Context", 1)
        body_part = parts[0]
        additional_context = parts[1].strip()
    else:
        body_part = content

    # Strip the "## Issue Body\n" header
    body = body_part.replace("## Issue Body\n", "").strip()

    return Task(
        id=meta.get("id", path.stem),
        source=meta.get("source", "unknown"),
        event_type=meta.get("event_type", "unknown"),
        summary=meta.get("summary", ""),
        body=body,
        issue_number=meta.get("issue_number"),
        priority=meta.get("priority", 0),
        retries=meta.get("retries", 0),
        max_retries=meta.get("max_retries", 3),
        branch_name=meta.get("branch_name"),
        pr_url=meta.get("pr_url"),
        error_log=meta.get("error_log"),
        created_at=meta.get("created_at", ""),
        started_at=meta.get("started_at"),
        completed_at=meta.get("completed_at"),
        additional_context=additional_context,
        status=status,
    )


def _slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    import re

    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")
