"""Central configuration from environment variables."""

import os


def _get_required(key: str) -> str:
    """Get a required environment variable or raise."""
    value = os.environ.get(key, "")
    if not value:
        raise RuntimeError(f"Required environment variable {key} is not set")
    return value


def _get_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).lower() in ("true", "1", "yes")


# --- Required ---
GITHUB_TOKEN: str = _get_required("GITHUB_TOKEN")
GITHUB_REPO: str = _get_required("GITHUB_REPO")  # owner/repo
GITHUB_WEBHOOK_SECRET: str = _get_required("GITHUB_WEBHOOK_SECRET")

# Claude billing
CLAUDE_USE_MAX: bool = _get_bool("CLAUDE_USE_MAX", False)
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

if not CLAUDE_USE_MAX and not ANTHROPIC_API_KEY:
    raise RuntimeError(
        "Either ANTHROPIC_API_KEY must be set or CLAUDE_USE_MAX must be true"
    )

# --- Optional ---
SLACK_WEBHOOK_URL: str = os.environ.get("SLACK_WEBHOOK_URL", "")

# --- Defaults ---
REPO_DIR: str = os.environ.get("REPO_DIR", "/data/repo")
TASKS_DIR: str = os.environ.get("TASKS_DIR", "/data/tasks")
PORT: int = int(os.environ.get("PORT", "5000"))
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "60"))
MAX_RETRIES_PER_TASK: int = int(os.environ.get("MAX_RETRIES_PER_TASK", "3"))
CLAUDE_TIMEOUT: int = int(os.environ.get("CLAUDE_TIMEOUT", "1800"))
TRIGGER_LABEL: str = os.environ.get("TRIGGER_LABEL", "claude")

# Derived
GITHUB_OWNER: str = GITHUB_REPO.split("/")[0]
GITHUB_REPO_NAME: str = GITHUB_REPO.split("/")[1]
