import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 잡별 별도 봇 (env에 비어있으면 기본 봇으로 fallback)
_PER_MODE_CREDS: dict[str, tuple[str, str]] = {
    "us": (os.getenv("TELEGRAM_BOT_TOKEN_US", ""), os.getenv("TELEGRAM_CHAT_ID_US", "")),
    "kr": (os.getenv("TELEGRAM_BOT_TOKEN_KR", ""), os.getenv("TELEGRAM_CHAT_ID_KR", "")),
    # pulse(시장 급변 원인 추적): 한국 시장 리포트와 같은 KR 채널로
    "pulse": (os.getenv("TELEGRAM_BOT_TOKEN_KR", ""), os.getenv("TELEGRAM_CHAT_ID_KR", "")),
    "kr_deepdive": (os.getenv("TELEGRAM_BOT_TOKEN_DEEPDIVE", ""), os.getenv("TELEGRAM_CHAT_ID_DEEPDIVE", "")),
    "kr_compare": (os.getenv("TELEGRAM_BOT_TOKEN_DEEPDIVE", ""), os.getenv("TELEGRAM_CHAT_ID_DEEPDIVE", "")),
    "insight": (os.getenv("TELEGRAM_BOT_TOKEN_INSIGHT", ""), os.getenv("TELEGRAM_CHAT_ID_INSIGHT", "")),
    # radar(글로벌 레이더): 전용 봇 없으면 insight 채널로, 그것도 없으면 기본 봇으로 fallback
    "radar": (
        os.getenv("TELEGRAM_BOT_TOKEN_RADAR", "") or os.getenv("TELEGRAM_BOT_TOKEN_INSIGHT", ""),
        os.getenv("TELEGRAM_CHAT_ID_RADAR", "") or os.getenv("TELEGRAM_CHAT_ID_INSIGHT", ""),
    ),
    "chart_lesson": (os.getenv("TELEGRAM_BOT_TOKEN_CHART", ""), os.getenv("TELEGRAM_CHAT_ID_CHART", "")),
    "note": (os.getenv("TELEGRAM_BOT_TOKEN_NOTE", ""), os.getenv("TELEGRAM_CHAT_ID_NOTE", "")),
    "consultant": (os.getenv("TELEGRAM_BOT_TOKEN_CONSULTANT", ""), os.getenv("TELEGRAM_CHAT_ID_CONSULTANT", "")),
    # macro / macro_daily: 별도 봇 없으면 insight 봇으로, 그것도 없으면 기본 봇으로 fallback
    "macro": (
        os.getenv("TELEGRAM_BOT_TOKEN_MACRO", "") or os.getenv("TELEGRAM_BOT_TOKEN_INSIGHT", ""),
        os.getenv("TELEGRAM_CHAT_ID_MACRO", "") or os.getenv("TELEGRAM_CHAT_ID_INSIGHT", ""),
    ),
    "macro_daily": (
        os.getenv("TELEGRAM_BOT_TOKEN_MACRO", "") or os.getenv("TELEGRAM_BOT_TOKEN_INSIGHT", ""),
        os.getenv("TELEGRAM_CHAT_ID_MACRO", "") or os.getenv("TELEGRAM_CHAT_ID_INSIGHT", ""),
    ),
    "monitor": (
        os.getenv("TELEGRAM_BOT_TOKEN_MONITOR", ""),
        os.getenv("TELEGRAM_CHAT_ID_MONITOR", ""),
    ),
}

NOTE_SHEET_URL = os.getenv("NOTE_SHEET_URL", "")


def get_credentials(mode: str | None = None) -> tuple[str, str]:
    """Return (token, chat_id) for `mode`, falling back to default."""
    if mode and mode in _PER_MODE_CREDS:
        token, chat_id = _PER_MODE_CREDS[mode]
        if token and chat_id:
            return token, chat_id
    return TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


# Claude Code CLI alias ("opus", "sonnet", "haiku") or full model name.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "opus")
# Optional: OpenDART API key for richer/safer fundamental data.
OPEN_DART_API_KEY = os.getenv("OPEN_DART_API_KEY", "")
WEEKDAY_ONLY = os.getenv("WEEKDAY_ONLY", "true").lower() == "true"

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)


def assert_env() -> None:
    missing = [
        name
        for name, value in [
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        ]
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing environment variables: {', '.join(missing)}. "
            f"Copy .env.example to .env and fill in the values."
        )
