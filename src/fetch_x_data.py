"""Read-only fetch helpers for twitterapi.io.

Free-tier limit is ~1 QPS (one request every 5 seconds). We enforce a
>5s gap between requests and retry once on HTTP 429.
"""

from __future__ import annotations

import os
import time
from typing import Any, Iterable

import requests

BASE_URL = "https://api.twitterapi.io"
MIN_INTERVAL_S = 5.5  # free-tier safety margin


class TwitterAPIError(RuntimeError):
    pass


_LAST_CALL_TS: float = 0.0


def _key() -> str:
    key = os.environ.get("TWITTERAPI_IO_KEY", "").strip()
    if not key:
        raise TwitterAPIError("TWITTERAPI_IO_KEY is not set")
    return key


def _respect_qps() -> None:
    global _LAST_CALL_TS
    elapsed = time.time() - _LAST_CALL_TS
    if elapsed < MIN_INTERVAL_S:
        time.sleep(MIN_INTERVAL_S - elapsed)
    _LAST_CALL_TS = time.time()


def _get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    _respect_qps()
    resp = requests.get(
        f"{BASE_URL}{path}",
        headers={"x-api-key": _key()},
        params=params,
        timeout=20,
    )
    if resp.status_code == 429:
        time.sleep(MIN_INTERVAL_S)
        _respect_qps()
        resp = requests.get(
            f"{BASE_URL}{path}",
            headers={"x-api-key": _key()},
            params=params,
            timeout=20,
        )
    if not resp.ok:
        raise TwitterAPIError(f"GET {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def get_user_info(username: str) -> dict[str, Any]:
    return _get("/twitter/user/info", {"userName": username})


def get_user_last_tweets(username: str, count: int = 20) -> list[dict[str, Any]]:
    payload = _get("/twitter/user/last_tweets", {"userName": username, "count": count})
    inner = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(inner, dict):
        tweets = inner.get("tweets")
        if isinstance(tweets, list):
            return tweets
    tweets = payload.get("tweets") if isinstance(payload, dict) else None
    return tweets if isinstance(tweets, list) else []


def _tweet_id_int(t: dict[str, Any]) -> int:
    try:
        return int(t.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def filter_new_tweets(tweets: Iterable[dict[str, Any]], last_id: str | None) -> list[dict[str, Any]]:
    """Return tweets with snowflake id strictly greater than last_id."""
    threshold = 0
    if last_id:
        try:
            threshold = int(last_id)
        except ValueError:
            threshold = 0
    out = [t for t in tweets if _tweet_id_int(t) > threshold]
    out.sort(key=_tweet_id_int)
    return out
