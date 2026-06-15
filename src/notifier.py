"""Telegram bot sender. Uses HTML parse mode with auto-split for >4096 chars."""
from __future__ import annotations

import logging
import re
import time
from typing import Callable

import certifi
import requests

from . import config

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot"
HARD_LIMIT = 4096
SAFE_LIMIT = 4000

# 시스템 OpenSSL 인증서 경로(환경마다 다름)가 아닌 certifi 번들을 명시적으로 사용한다.
# 일부 환경에서 "unable to get local issuer certificate" 검증 실패를 줄인다.
_CA_BUNDLE = certifi.where()

# 일시적 네트워크 장애(SSL 핸드셰이크 실패, DNS 미해결, 연결 리셋, 타임아웃)는
# wake-from-sleep / 네트워크 전환 직후에 자주 발생한다. 백오프하며 재시도해 흡수한다.
_RETRY_BACKOFF_SECONDS = (5, 15)  # 시도 사이 대기 → 총 3회 시도
_TRANSIENT_NETWORK_ERRORS = (
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


def _request_with_retry(do_request: Callable[[], requests.Response]) -> requests.Response:
    """do_request()를 호출하고 일시적 네트워크 오류면 백오프 후 재시도한다.

    HTTP 상태 오류(4xx/5xx)는 여기서 다루지 않는다 — 호출부가 status_code로 처리.
    do_request는 재시도마다 새로 실행되므로 파일 핸들 등은 클로저 안에서 새로 열어야 한다.
    """
    last_err: Exception | None = None
    total_attempts = len(_RETRY_BACKOFF_SECONDS) + 1
    for attempt in range(1, total_attempts + 1):
        try:
            return do_request()
        except _TRANSIENT_NETWORK_ERRORS as e:
            last_err = e
            if attempt < total_attempts:
                wait = _RETRY_BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    "telegram network error (%s, attempt %d/%d), retrying in %ds",
                    type(e).__name__, attempt, total_attempts, wait,
                )
                time.sleep(wait)
    assert last_err is not None
    raise last_err


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

    r = _request_with_retry(
        lambda: requests.post(url, json=payload, timeout=30, verify=_CA_BUNDLE)
    )
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
    r = _request_with_retry(lambda: requests.get(url, timeout=10, verify=_CA_BUNDLE))
    r.raise_for_status()
    return r.json()


def send_photo(
    photo_path: str,
    caption: str | None = None,
    parse_mode: str | None = "HTML",
    mode: str | None = None,
) -> dict:
    """Send a photo with optional HTML caption (max 1024 chars per Telegram).

    Retries transient network errors (wake-from-sleep can leave network slow/unresolved).
    """
    token, chat_id = config.get_credentials(mode)
    url = f"{API_BASE}{token}/sendPhoto"
    data = {"chat_id": chat_id, "disable_notification": False}
    if caption:
        data["caption"] = caption[:1024]
        if parse_mode:
            data["parse_mode"] = parse_mode

    # 파일 핸들은 재시도마다 새로 열어야 하므로 클로저 안에서 open한다.
    def _post() -> requests.Response:
        with open(photo_path, "rb") as f:
            return requests.post(
                url, files={"photo": f}, data=data, timeout=180, verify=_CA_BUNDLE
            )

    r = _request_with_retry(_post)
    if r.status_code != 200:
        logger.error("sendPhoto failed %s: %s", r.status_code, r.text)
        r.raise_for_status()
    return r.json()
