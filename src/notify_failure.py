"""Launchd 잡 실패 알림 — run.sh 류 wrapper가 비정상 종료 시 호출.

환경변수로 컨텍스트를 받아 모니터 채널로 텔레그램 알림을 보낸다.
notifier만 의존하므로 claude/analyst가 깨져 있어도 호출 가능하다.

env:
    JOB       잡 라벨 (예: kr, macro_daily, monitor_intraday)
    EXIT      python 종료 코드
    ERR_LOG   에러 로그 경로 (없거나 비었으면 본문에 그 사실 표기)
    TAIL_BYTES 본문에 포함할 마지막 바이트 수 (기본 1500)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import pytz

from .notifier import send_message

_KST = pytz.timezone("Asia/Seoul")
_MAX_TAIL_BYTES = 1500


def _read_tail(path: str, max_bytes: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)  # end
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            chunk = f.read()
        return chunk.decode("utf-8", errors="replace").strip()
    except FileNotFoundError:
        return "(err log not found)"
    except Exception as e:  # pragma: no cover — 알림 경로는 절대 죽으면 안 됨
        return f"(failed to read err log: {e})"


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main() -> int:
    job = os.environ.get("JOB", "unknown")
    exit_code = os.environ.get("EXIT", "?")
    err_log = os.environ.get("ERR_LOG", "")
    max_bytes = int(os.environ.get("TAIL_BYTES", _MAX_TAIL_BYTES))

    ts = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S KST")
    tail = _read_tail(err_log, max_bytes) if err_log else "(no err log path)"
    tail = _escape_html(tail) or "(empty)"

    msg = (
        f"❌ <b>잡 실패: {_escape_html(job)}</b>\n"
        f"{ts} · exit={_escape_html(str(exit_code))}\n"
        f"<pre>{tail}</pre>"
    )

    try:
        send_message(msg, mode="monitor")
        return 0
    except Exception as e:
        # 알림 자체가 실패 — stderr로 남겨 launchd .err에 기록되게
        sys.stderr.write(f"notify_failure: send_message failed: {type(e).__name__}: {e}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
