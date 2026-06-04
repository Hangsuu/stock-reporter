"""Bot-controllable launchd schedule + .env updates.

Hard guardrails:
- Never modifies the bot's own plist (would break the channel that issues commands).
- Never edits files outside the project directory or ~/Library/LaunchAgents/com.user.stockreporter.*.
- Only known model aliases are accepted for CLAUDE_MODEL.
- Only known job aliases are accepted for /schedule, /run.
"""
from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

JOB_LABELS = {
    "us": "com.user.stockreporter.us",
    "us_top20": "com.user.stockreporter.us_top20",
    "kr": "com.user.stockreporter.kr",
    "kr_top20": "com.user.stockreporter.kr_top20",
    "deepdive": "com.user.stockreporter.kr_deepdive",
    "insight": "com.user.stockreporter.insight",
    "radar": "com.user.stockreporter.radar",
    "chart": "com.user.stockreporter.chart_lesson",
    "macro": "com.user.stockreporter.macro",
    "macro_daily": "com.user.stockreporter.macro_daily",
    "monitor_intraday": "com.user.stockreporter.monitor_intraday",
    "monitor_daily": "com.user.stockreporter.monitor_daily",
}
SELF_LABEL = "com.user.stockreporter.bot"  # never touched by /schedule

PROJECT_DIR = Path(__file__).resolve().parent.parent
PLIST_DIR = PROJECT_DIR / "launchd"
LAUNCH_AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
LOGS_DIR = PROJECT_DIR / "logs"

WEEKDAY_JOBS = {"us", "us_top20", "kr", "kr_top20", "deepdive", "macro_daily", "monitor_daily"}
DAILY_JOBS = {"insight", "chart"}
WEEKLY_JOBS = {"macro"}  # 주 1회 (요일은 plist에서 고정, /schedule은 시간만 변경)

VALID_MODELS = {
    "opus", "sonnet", "haiku",
    "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
}


def _job_label(job: str) -> str:
    if job not in JOB_LABELS:
        raise ValueError(f"unknown job '{job}'. valid: {sorted(JOB_LABELS)}")
    return JOB_LABELS[job]


_WEEKDAY_KO = {0: "일", 1: "월", 2: "화", 3: "수", 4: "목", 5: "금", 6: "토", 7: "일"}


def _format_schedule(sci) -> str:
    if isinstance(sci, list) and sci:
        weekdays = sorted({d.get("Weekday") for d in sci if d.get("Weekday") is not None})
        times = sorted({f"{d.get('Hour', 0):02d}:{d.get('Minute', 0):02d}" for d in sci})
        times_str = ",".join(times)
        if not weekdays:  # 요일 없는 멀티타임 (예: radar 매일 08:00,20:00)
            return f"매일 {times_str}"
        days_str = "평일" if weekdays == [1, 2, 3, 4, 5] else "주" + ",".join(map(str, weekdays))
        return f"{days_str} {times_str}"
    if isinstance(sci, dict):
        if "Weekday" in sci:
            wd = _WEEKDAY_KO.get(sci["Weekday"], str(sci["Weekday"]))
            return f"매주 {wd} {sci.get('Hour', 0):02d}:{sci.get('Minute', 0):02d}"
        return f"매일 {sci.get('Hour', 0):02d}:{sci.get('Minute', 0):02d}"
    return "상시"


def list_schedules() -> list[dict]:
    out = []
    for job, label in JOB_LABELS.items():
        plist_path = PLIST_DIR / f"{label}.plist"
        if not plist_path.exists():
            out.append({"job": job, "label": label, "schedule": "(plist 없음)"})
            continue
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        out.append({
            "job": job,
            "label": label,
            "schedule": _format_schedule(data.get("StartCalendarInterval")),
        })
    out.append({"job": "bot", "label": SELF_LABEL, "schedule": "상시 (수정 불가)"})
    return out


def update_schedule(job: str, hour: int, minute: int) -> str:
    label = _job_label(job)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"잘못된 시간: {hour:02d}:{minute:02d}")

    plist_path = PLIST_DIR / f"{label}.plist"
    if not plist_path.exists():
        raise FileNotFoundError(str(plist_path))

    with open(plist_path, "rb") as f:
        data = plistlib.load(f)

    if job in WEEKDAY_JOBS:
        data["StartCalendarInterval"] = [
            {"Weekday": i, "Hour": hour, "Minute": minute}
            for i in range(1, 6)
        ]
    elif job in DAILY_JOBS:
        data["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
    elif job in WEEKLY_JOBS:
        # 기존 plist의 Weekday는 보존, 시간만 변경
        existing = data.get("StartCalendarInterval", {})
        if isinstance(existing, dict):
            weekday = existing.get("Weekday", 0)  # 기본: 일요일
        else:
            weekday = 0
        data["StartCalendarInterval"] = {"Weekday": weekday, "Hour": hour, "Minute": minute}
    else:
        raise ValueError(f"job '{job}'은 시간 변경 대상이 아닙니다")

    with open(plist_path, "wb") as f:
        plistlib.dump(data, f)

    deployed = LAUNCH_AGENT_DIR / f"{label}.plist"
    shutil.copy(plist_path, deployed)
    subprocess.run(["launchctl", "unload", str(deployed)], check=False, capture_output=True)
    subprocess.run(["launchctl", "load", str(deployed)], check=True, capture_output=True)
    logger.info("Schedule updated: %s → %02d:%02d", job, hour, minute)
    return f"{job} → {hour:02d}:{minute:02d}"


def trigger_job(job: str) -> str:
    label = _job_label(job)
    uid = os.getuid()
    target = f"gui/{uid}/{label}"
    res = subprocess.run(
        ["launchctl", "kickstart", "-k", target],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"kickstart failed: {res.stderr.strip() or res.stdout.strip()}")
    return f"{job} 실행 트리거됨"


def update_model(model: str) -> str:
    if model not in VALID_MODELS:
        raise ValueError(f"unknown model '{model}'. valid: {sorted(VALID_MODELS)}")

    env_path = PROJECT_DIR / ".env"
    if not env_path.exists():
        raise FileNotFoundError(".env not found")

    lines = env_path.read_text().splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith("CLAUDE_MODEL="):
            new_lines.append(f"CLAUDE_MODEL={model}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"CLAUDE_MODEL={model}")

    env_path.write_text("\n".join(new_lines) + "\n")
    logger.info("Model updated to %s", model)
    return f"CLAUDE_MODEL → {model} (다음 분석부터 적용)"


def tail_log(job: str, lines: int = 15) -> str:
    label_to_logname = {
        "us": "launchd_us", "us_top20": "launchd_us_top20",
        "kr": "launchd_kr", "kr_top20": "launchd_kr_top20",
        "deepdive": "launchd_kr_deepdive",
        "insight": "launchd_insight", "radar": "launchd_radar",
        "chart": "launchd_chart_lesson", "bot": "launchd_bot",
    }
    if job not in label_to_logname:
        raise ValueError(f"unknown job '{job}'. valid: {sorted(label_to_logname)}")
    log_path = LOGS_DIR / f"{label_to_logname[job]}.log"
    if not log_path.exists():
        return f"(로그 없음: {log_path.name})"
    content = log_path.read_text(errors="replace").splitlines()
    return "\n".join(content[-lines:])
