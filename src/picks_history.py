"""Track recently-picked tickers so chart_lesson / kr_deepdive don't repeat themselves.

Stored at logs/picks_history.json as a list of {market, ticker, name, date}.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Iterable

import pytz

from . import config

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

HISTORY_PATH = config.LOG_DIR / "picks_history.json"
_lock = threading.Lock()


def _load() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load picks_history: %s", e)
        return []


def _save(records: list[dict]) -> None:
    HISTORY_PATH.parent.mkdir(exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def record_pick(market: str, ticker: str, name: str) -> None:
    today = datetime.now(KST).date().isoformat()
    with _lock:
        records = _load()
        records.append({"market": market, "ticker": ticker, "name": name, "date": today})
        # Keep last 365 days only to avoid unbounded growth
        cutoff = (datetime.now(KST).date() - timedelta(days=365)).isoformat()
        records = [r for r in records if r.get("date", "") >= cutoff]
        _save(records)
    logger.info("Recorded pick: market=%s ticker=%s name=%s", market, ticker, name)


def recent_tickers(market: str, days: int) -> set[str]:
    cutoff = (datetime.now(KST).date() - timedelta(days=days - 1)).isoformat()
    with _lock:
        records = _load()
    return {
        r["ticker"]
        for r in records
        if r.get("market") == market and r.get("date", "") >= cutoff
    }


def filter_recent(market: str, candidates: Iterable, days: int, key=lambda c: c) -> list:
    """Remove recently-picked tickers from candidate iterable.

    `key` extracts the ticker string from each item (default: identity).
    Returns a new list. If all candidates were recently picked, returns
    the original list (so the system never deadlocks).
    """
    blocked = recent_tickers(market, days)
    if not blocked:
        return list(candidates)
    fresh = [c for c in candidates if key(c) not in blocked]
    if not fresh:
        logger.warning("All %s candidates were recently picked — falling back to full pool", market)
        return list(candidates)
    return fresh
