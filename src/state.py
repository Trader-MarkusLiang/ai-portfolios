"""Persisted per-handle state: last seen tweet id and timestamp."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def update_handle(state: dict[str, dict[str, Any]], handle: str, last_tweet_id: str, now_iso: str) -> None:
    state[handle] = {"last_tweet_id": str(last_tweet_id), "last_seen_at": now_iso}
