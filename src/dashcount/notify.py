"""Phone alerts through ntfy (https://ntfy.sh). Off unless NTFY_TOPIC is set.

Install the ntfy app, subscribe to your topic (a long random name acts as the
password), and put NTFY_TOPIC=<that name> in .env.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


def send(message: str, title: str = "DashCount", priority: str = "default") -> None:
    log.info("ALERT: %s", message)
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        httpx.post(f"{server}/{topic}", content=message.encode("utf-8"),
                   headers={"Title": title, "Priority": priority}, timeout=10)
    except Exception as e:
        log.warning("ntfy failed: %s", e)
