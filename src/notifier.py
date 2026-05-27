"""Telegram bot sender. Uses HTML parse mode with auto-split for >4096 chars."""
from __future__ import annotations

import logging
import re
import time

import requests

from . import config

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot"
HARD_LIMIT = 4096
SAFE_LIMIT = 4000


def send_message(text: str, parse_mode: str | None = "HTML", mode: str | None = None) -> list[dict]:
    if not text or not text.strip():
        return []
    token, chat_id = config.get_credentials(mode)
    if parse_mode == "HTML":
        text = _escape_unsafe_lt(text)
    chunks = _split(text, SAFE_LIMIT)
    results = []
    for i, chunk in enumerate(chunks):
        if i > 0:
            time.sleep(0.5)
        try:
            results.append(_send_one(chunk, parse_mode, token, chat_id))
        except requests.HTTPError as e:
            logger.warning("HTML send failed (chunk %d), retrying as plain text: %s", i, e)
            results.append(_send_one(_strip_tags(chunk), None, token, chat_id))
    return results


def _send_one(text: str, parse_mode: str | None, token: str, chat_id: str) -> dict:
    url = f"{API_BASE}{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    # Retry once on network timeout (wake-from-sleep can leave network slow).
    for attempt in (1, 2):
        try:
            r = requests.post(url, json=payload, timeout=30)
            break
        except requests.exceptions.Timeout:
            if attempt == 2:
                raise
            logger.warning("sendMessage timed out (attempt 1/2), retrying after 5s...")
            time.sleep(5)
    if r.status_code != 200:
        logger.error("Telegram API error %s: %s", r.status_code, r.text)
        r.raise_for_status()
    return r.json()


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
            continue

        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# Telegram HTML allowed tag names (per Bot API docs)
_ALLOWED_TAGS = ("b", "i", "u", "s", "code", "pre", "a", "tg-spoiler", "blockquote",
                 "B", "I", "U", "S", "CODE", "PRE", "A", "strong", "em")
_ALLOWED_TAG_RE = re.compile(
    r"<\/?(?:" + "|".join(_ALLOWED_TAGS) + r")(?:\s[^>]*)?\/?>"
)


def _escape_unsafe_lt(text: str) -> str:
    """Escape `<` that isn't part of an allowed Telegram HTML tag.

    Claude often writes things like `<0.3배` or `<5%` in analysis text.
    Telegram's HTML parser then complains "Unsupported start tag '0.3'".
    """
    # Replace allowed tags with placeholders, escape stray <, then restore.
    placeholders: list[str] = []

    def stash(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"\x00TG_TAG_{len(placeholders) - 1}\x00"

    masked = _ALLOWED_TAG_RE.sub(stash, text)
    masked = masked.replace("<", "&lt;")

    def restore(m: re.Match) -> str:
        return placeholders[int(m.group(1))]

    return re.sub(r"\x00TG_TAG_(\d+)\x00", restore, masked)


def get_me() -> dict:
    url = f"{API_BASE}{config.TELEGRAM_BOT_TOKEN}/getMe"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def send_photo(
    photo_path: str,
    caption: str | None = None,
    parse_mode: str | None = "HTML",
    mode: str | None = None,
) -> dict:
    """Send a photo with optional HTML caption (max 1024 chars per Telegram).

    Retries once on network timeout (wake-from-sleep can leave network slow).
    """
    token, chat_id = config.get_credentials(mode)
    url = f"{API_BASE}{token}/sendPhoto"
    data = {"chat_id": chat_id, "disable_notification": False}
    if caption:
        data["caption"] = caption[:1024]
        if parse_mode:
            data["parse_mode"] = parse_mode

    for attempt in (1, 2):
        try:
            with open(photo_path, "rb") as f:
                r = requests.post(url, files={"photo": f}, data=data, timeout=180)
            break
        except requests.exceptions.Timeout:
            if attempt == 2:
                raise
            logger.warning("sendPhoto timed out (attempt 1/2), retrying after 5s...")
            time.sleep(5)
    if r.status_code != 200:
        logger.error("sendPhoto failed %s: %s", r.status_code, r.text)
        r.raise_for_status()
    return r.json()
