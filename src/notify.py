"""Slack notifications via incoming webhook."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


def send(message: str) -> None:
    """Send a notification to Slack (if configured) and log it."""
    logger.info("Notification: %s", message)

    # Lazy import to avoid circular dependency
    from src import config

    if not config.SLACK_WEBHOOK_URL:
        return

    try:
        resp = httpx.post(
            config.SLACK_WEBHOOK_URL,
            json={"text": message},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception:
        logger.exception("Failed to send Slack notification")
