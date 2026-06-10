"""Telegram long-polling bot.

Two bots can listen simultaneously:
- Main bot (TELEGRAM_BOT_TOKEN): full features (commands, chat, ticker analysis)
- Deepdive bot (TELEGRAM_BOT_TOKEN_DEEPDIVE): ticker matching only — sends a
  request, gets the deep-dive report posted back to the same Deepdive channel.

Only messages from `config.TELEGRAM_CHAT_ID` are honored (DM-style).

Run with: ./venv/bin/python -m src.bot
"""
from __future__ import annotations

import logging
import re
import sys
import threading
import time
from datetime import datetime
from typing import Callable

import pytz
import requests

from . import config, reporter, scheduler
from .analyst import CONSULTANT_SYSTEM_PROMPT, chat as claude_chat
from .data_kr import get_krx_listing
from .notifier import send_message

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

POLL_TIMEOUT = 30
MAX_CHOICES = 10

# 봇별로 받을 슬래시 명령. main은 _handle_command의 모든 명령을 받고(필터 X),
# 나머지 봇은 "main에서 입력했을 때 결과가 그 봇 채널로 가는" 명령만 받는다.
# 응답은 _handle_command(reply_mode=...)로 입력받은 봇 채널에 회신된다.
_DEEPDIVE_BOT_COMMANDS = frozenset({"/run"})
_NOTE_BOT_COMMANDS = frozenset({"/status", "/update", "/retarget"})
# /status, /check은 모니터 봇 핸들러에서 직접 처리(같은 채널 중복 confirmation 회피).
_MONITOR_BOT_COMMANDS = frozenset({"/update", "/retarget"})

_listing_cache: dict[str, str] | None = None
_listing_lock = threading.Lock()

# 봇별 대기 후보 — key는 (bot_label, chat_id)
_pending_choices: dict[tuple[str, str], list[tuple[str, str]]] = {}
_pending_lock = threading.Lock()


def _setup_logging() -> None:
    log_path = config.LOG_DIR / f"bot_{datetime.now(KST).strftime('%Y%m')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _name_to_code() -> dict[str, str]:
    global _listing_cache
    with _listing_lock:
        if _listing_cache is None:
            df = get_krx_listing()
            df = df[df["Market"].isin(["KOSPI", "KOSDAQ", "KONEX"])]
            _listing_cache = dict(zip(df["Name"], df["Code"]))
    return _listing_cache


def search_tickers(query: str, max_results: int = MAX_CHOICES) -> list[tuple[str, str]]:
    q = query.strip()
    if not q:
        return []
    if q.isdigit() and len(q) == 6:
        for name, code in _name_to_code().items():
            if code == q:
                return [(q, name)]
        return [(q, q)]
    mapping = _name_to_code()
    exact, starts, contains = [], [], []
    for name, code in mapping.items():
        if name == q:
            exact.append((code, name))
        elif name.startswith(q):
            starts.append((code, name))
        elif q in name:
            contains.append((code, name))
    starts.sort(key=lambda x: x[1])
    contains.sort(key=lambda x: x[1])
    return (exact + starts + contains)[:max_results]


def resolve_ticker(query: str) -> tuple[str, str] | None:
    results = search_tickers(query, max_results=2)
    if not results:
        return None
    if len(results) == 1:
        return results[0]
    if results[0][1] == query.strip():
        return results[0]
    return None


_PRICE_RE = re.compile(r"^(\d[\d,]*\.?\d*)(만원|만|원)?$")


def _parse_price(token: str) -> float | None:
    """'263000', '26.3만', '26만원', '263,000원' → float (원). 100 미만은 None."""
    m = _PRICE_RE.match(token.strip())
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = m.group(2) or ""
    if suffix in ("만", "만원"):
        v *= 10000
    if v < 100:  # 종목코드 부분 등 오인 방지
        return None
    return v


def parse_query_with_price(text: str) -> tuple[str, float | None]:
    """'삼성전자 263000' / '005930 26.3만' → ('삼성전자', 263000) etc.

    Returns (query, price). price=None when no trailing price detected.
    Six-digit ticker is NOT treated as a price (so '005930' stays a ticker).
    """
    text = text.strip()
    parts = text.split()
    if len(parts) < 2:
        return text, None

    last = parts[-1]
    price = _parse_price(last)
    if price is None:
        return text, None
    query = " ".join(parts[:-1]).strip()
    if not query:
        return text, None
    return query, price


# 종목명 뒤에 붙이는 명령 키워드 → 모드 매핑
_COMMAND_KEYWORDS = {
    "분기실적": "kr_quarterly",
    "분기 실적": "kr_quarterly",
    "재무제표": "kr_quarterly",
    "종토방": "kr_board",
    "토론방": "kr_board",
    "분위기": "kr_board",
}


def _resolve_ticker_or_code(query: str) -> tuple[str, str] | None:
    """6자리 코드면 그대로 + 종목명 조회. 아니면 종목명으로 정확 매칭 검색.

    /update /retarget용. 부분 매칭은 안 함 (모호성 회피).
    """
    q = query.strip()
    # 5~6자리 숫자 → 6자리 padding
    if q.isdigit() and 1 <= len(q) <= 6:
        code = q.zfill(6)
        try:
            df = get_krx_listing()
            row = df[df["Code"] == code]
            if not row.empty:
                return code, str(row.iloc[0]["Name"])
        except Exception:
            pass
        return code, code
    # 종목명 정확 매칭
    res = resolve_ticker(q)
    return res


def parse_query_with_command(text: str) -> tuple[str, str | None]:
    """'삼성전자 분기실적' → ('삼성전자', 'kr_quarterly'). 없으면 (text, None)."""
    text = text.strip()
    for kw, mode in _COMMAND_KEYWORDS.items():
        if text.endswith(kw):
            query = text[: -len(kw)].strip()
            if query:
                return query, mode
    return text, None


_COMPARE_SEP_RE = re.compile(r"\s+(?:vs\.?|VS\.?|대|v\.|versus)\s+", re.IGNORECASE)


def parse_compare_query(text: str) -> tuple[str, str] | None:
    """'삼성전자 vs SK하이닉스' → ('삼성전자', 'SK하이닉스'). 없으면 None."""
    parts = _COMPARE_SEP_RE.split(text.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(), parts[1].strip()
    if a and b:
        return a, b
    return None


def _start_analysis(
    chat_id: str,
    ticker: str,
    name: str,
    *,
    reply_mode: str | None,
    entry_price: float | None = None,
    command_mode: str | None = None,
) -> None:
    """Send 'analyzing' notice, then run kr_deepdive (or quarterly variant)."""
    if command_mode in ("kr_quarterly", "kr_board"):
        label = "분기실적 분석" if command_mode == "kr_quarterly" else "종토방 분위기 분석"
        emoji = "📊" if command_mode == "kr_quarterly" else "💬"
        send_message(
            f"{emoji} <b>{name}</b> (<code>{ticker}</code>) {label} 중... 약 1~2분\n"
            f"결과는 <b>@HarrisonStockDeepdive_bot</b> 채널에 도착합니다.",
            mode=reply_mode,
        )
        logger.info("%s %s (%s) mode=%s chat=%s", command_mode, name, ticker, reply_mode, chat_id)
        try:
            reporter.run(command_mode, skip_weekend_check=True, forced_ticker=ticker)
        except Exception as e:
            logger.exception("%s failed", command_mode)
            send_message(
                f"⚠️ {label} 실패: <code>{type(e).__name__}: {e}</code>",
                mode=reply_mode,
            )
        return

    price_note = f" / 진입가 <code>{int(entry_price):,}원</code> 기준 분석" if entry_price else ""
    send_message(
        f"🔍 <b>{name}</b> (<code>{ticker}</code>){price_note} 분석 중... 약 1~2분\n"
        f"결과는 <b>@HarrisonStockDeepdive_bot</b> 채널에 도착합니다.",
        mode=reply_mode,
    )
    logger.info(
        "Analyzing %s (%s) entry=%s mode=%s chat=%s",
        name, ticker, entry_price, reply_mode, chat_id,
    )
    try:
        reporter.run(
            "kr_deepdive",
            skip_weekend_check=True,
            forced_ticker=ticker,
            user_entry_price=entry_price,
        )
    except Exception as e:
        logger.exception("analysis failed")
        send_message(
            f"⚠️ 분석 실패: <code>{type(e).__name__}: {e}</code>",
            mode=reply_mode,
        )


def _try_pick_from_choices(bot_label: str, chat_id: str, text: str, *, reply_mode: str | None) -> bool:
    if not (text.isdigit() and 1 <= int(text) <= MAX_CHOICES):
        return False
    n = int(text)
    with _pending_lock:
        choices = _pending_choices.get((bot_label, chat_id))
    if not choices or n > len(choices):
        return False
    ticker, name = choices[n - 1]
    with _pending_lock:
        _pending_choices.pop((bot_label, chat_id), None)
    _start_analysis(chat_id, ticker, name, reply_mode=reply_mode)
    return True


def _try_resolve_or_list(bot_label: str, chat_id: str, text: str, *, reply_mode: str | None) -> bool:
    """Single match → analyze. Multi match → show list. No match → False.

    Supports:
    - "삼성전자 263000" → entry_price
    - "삼성전자 분기실적" → quarterly mode
    """
    # 명령어 키워드 먼저 (가격보다 우선)
    query, command_mode = parse_query_with_command(text)
    entry_price = None
    if not command_mode:
        query, entry_price = parse_query_with_price(text)

    resolved = resolve_ticker(query)
    if resolved:
        with _pending_lock:
            _pending_choices.pop((bot_label, chat_id), None)
        ticker, name = resolved
        _start_analysis(
            chat_id, ticker, name,
            reply_mode=reply_mode,
            entry_price=entry_price,
            command_mode=command_mode,
        )
        return True

    candidates = search_tickers(query, max_results=MAX_CHOICES)
    if candidates:
        with _pending_lock:
            _pending_choices[(bot_label, chat_id)] = candidates
        lines = [f"🔎 '<code>{query}</code>' 관련 종목 {len(candidates)}건:"]
        for i, (code, name) in enumerate(candidates, 1):
            lines.append(f"{i}. {name} (<code>{code}</code>)")
        lines.append("")
        if entry_price:
            lines.append(f"👉 번호 또는 정확한 종목명/코드를 보내주세요. (진입가 <code>{int(entry_price):,}원</code> 적용은 정확한 종목 입력 시)")
        else:
            lines.append("👉 번호 또는 정확한 종목명/6자리 코드를 보내주세요.")
        send_message("\n".join(lines), mode=reply_mode)
        return True
    return False


# ── 핸들러: 메인 봇 (전체 기능) ─────────────────────────────────────────
def _handle_command(text: str, reply_mode: str | None = None) -> bool:
    """슬래시 명령 디스패치. 응답은 reply_mode 봇 채널로 간다 (None=메인).

    호출 측에서 봇별로 허용 명령을 좁히려면 본 함수 호출 전에 미리 cmd를 필터링한다.
    """
    parts = text.split()
    cmd = parts[0].lower()

    def send(msg: str) -> None:
        send_message(msg, mode=reply_mode)

    if cmd == "/schedule":
        if len(parts) == 2 and parts[1] == "list":
            schedules = scheduler.list_schedules()
            lines = ["📅 <b>현재 자동 일정</b>"]
            for s in schedules:
                lines.append(f"▸ <b>{s['job']:10s}</b> {s['schedule']}")
            lines.append("\n변경: <code>/schedule [us|us_top20|kr|kr_top20|deepdive|insight|chart|macro] HH:MM</code>")
            send("\n".join(lines))
            return True
        if len(parts) == 3:
            job = parts[1].lower()
            m = re.match(r"^(\d{1,2}):(\d{2})$", parts[2])
            if not m:
                send(f"❌ 시간 형식 오류: <code>{parts[2]}</code>. 'HH:MM' 필요")
                return True
            try:
                result = scheduler.update_schedule(job, int(m.group(1)), int(m.group(2)))
                send(f"✅ <b>{result}</b> 변경 완료")
            except Exception as e:
                send(f"❌ <code>{type(e).__name__}: {e}</code>")
            return True
        send(
            "사용법:\n<code>/schedule list</code>\n<code>/schedule us 07:00</code>\n<code>/schedule insight 09:30</code>"
        )
        return True

    if cmd == "/run":
        if len(parts) != 2:
            send(
                "사용법: <code>/run [us|us_top20|kr|kr_top20|deepdive|insight|radar|pulse|chart|macro]</code>\n"
                "  • <b>pulse</b> = 지금 시장 급변(급락/급등) 원인 추적 — 지표·속보·찌라시 분석"
            )
            return True
        try:
            send(f"⚡ {scheduler.trigger_job(parts[1].lower())}. 결과는 1~3분 내 도착")
        except Exception as e:
            send(f"❌ <code>{type(e).__name__}: {e}</code>")
        return True

    if cmd == "/model":
        if len(parts) != 2:
            send("사용법: <code>/model [opus|sonnet|haiku]</code>")
            return True
        try:
            send(f"✅ {scheduler.update_model(parts[1].lower())}")
        except Exception as e:
            send(f"❌ <code>{type(e).__name__}: {e}</code>")
        return True

    if cmd == "/status":
        try:
            from . import monitor as mon
            mon.daily_report()
            send("✅ 포지션 리포트는 모니터 봇 채널로 전송했습니다.")
        except Exception as e:
            send(f"❌ <code>{type(e).__name__}: {e}</code>")
        return True

    if cmd == "/check":
        try:
            from . import monitor as mon
            mon.check_intraday()
            send("✅ 장중 TP/SL 체크 완료. 도달 종목이 있으면 모니터 봇 채널로 알림.")
        except Exception as e:
            send(f"❌ <code>{type(e).__name__}: {e}</code>")
        return True

    if cmd == "/update":
        # /update 005930|삼성전자 tp1=320000 sl=295000
        if len(parts) < 3:
            send(
                "사용법: <code>/update 005930 tp1=320000 [tp2=350000] [sl=295000]</code>\n"
                "또는 종목명: <code>/update 삼성전자 tp1=320000</code>\n"
                "지정한 키만 수정 (기존 값 보존). 시트는 평단 대비 % 자동 계산."
            )
            return True
        ticker_q = parts[1].strip()
        resolved = _resolve_ticker_or_code(ticker_q)
        if not resolved:
            send(f"❌ '<code>{ticker_q}</code>' 종목 찾지 못함")
            return True
        ticker, name = resolved
        kwargs: dict = {}
        for kv in parts[2:]:
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            k = k.lower().strip()
            try:
                v_num = float(v.replace(",", "").strip())
            except ValueError:
                continue
            if k in ("tp1", "tp2", "sl"):
                kwargs[k] = v_num
        if not kwargs:
            send("❌ 수정할 키 없음 (tp1=, tp2=, sl= 중 하나 이상 필요)")
            return True
        try:
            from . import monitor as mon
            result = mon.update_targets(ticker, **kwargs)
            if result.get("ok"):
                avg = result.get("avg_price")
                lines = [f"✅ <b>{name}</b> (<code>{ticker}</code>) 수정 완료"]
                if avg:
                    lines.append(f"   평단 <code>{int(avg):,}원</code> 기준")
                for k, v in kwargs.items():
                    pct_str = ""
                    if avg:
                        p = (v - avg) / avg * 100
                        pct_str = f" ({'+' if p >= 0 else ''}{p:.1f}%)"
                    lines.append(f"   {k.upper()} <b>{int(v):,}원</b>{pct_str}")
                send("\n".join(lines))
            else:
                send(f"❌ {result.get('error') or '실패'}: <pre>{result}</pre>")
        except Exception as e:
            send(f"❌ <code>{type(e).__name__}: {e}</code>")
        return True

    if cmd == "/retarget":
        # /retarget 005930|삼성전자 [tp1_pct tp2_pct sl_pct]
        if len(parts) < 2:
            send(
                "사용법: <code>/retarget 005930</code> (기본 7/15/-5%)\n"
                "또는 종목명: <code>/retarget 삼성전자 10 20 -3</code>"
            )
            return True
        ticker_q = parts[1].strip()
        resolved = _resolve_ticker_or_code(ticker_q)
        if not resolved:
            send(f"❌ '<code>{ticker_q}</code>' 종목 찾지 못함")
            return True
        ticker, name = resolved
        kwargs: dict = {}
        if len(parts) >= 5:
            try:
                kwargs["tp1_pct"] = float(parts[2])
                kwargs["tp2_pct"] = float(parts[3])
                kwargs["sl_pct"] = float(parts[4])
            except ValueError:
                send("❌ % 값은 숫자여야 합니다. 예: <code>/retarget 삼성전자 10 20 -3</code>")
                return True
        try:
            from . import monitor as mon
            result = mon.retarget(ticker, **kwargs)
            if result.get("ok"):
                tp1p, tp2p, slp = result["pcts"]
                avg = result.get("avg_price")
                lines = [
                    f"✅ <b>{name}</b> (<code>{ticker}</code>) 재설정 완료",
                    f"   현재가 <b>{int(result['current']):,}원</b>",
                ]
                if avg:
                    lines.append(f"   평단 <code>{int(avg):,}원</code> (시트 저장 % 기준)")
                lines += [
                    f"   🎯 TP1 <b>{int(result['tp1']):,}원</b> (현재가 {tp1p:+g}%)",
                    f"   🎯 TP2 <b>{int(result['tp2']):,}원</b> (현재가 {tp2p:+g}%)",
                    f"   ⚠️ SL <b>{int(result['sl']):,}원</b> (현재가 {slp:+g}%)",
                ]
                send("\n".join(lines))
            else:
                send(f"❌ {result.get('error') or '실패'}: <pre>{result}</pre>")
        except Exception as e:
            send(f"❌ <code>{type(e).__name__}: {e}</code>")
        return True

    if cmd == "/logs":
        if len(parts) != 2:
            send("사용법: <code>/logs [us|us_top20|kr|kr_top20|deepdive|insight|radar|pulse|chart|macro|bot]</code>")
            return True
        try:
            content = scheduler.tail_log(parts[1].lower(), lines=15)
            send(f"📜 <b>{parts[1]} 로그 (최근 15줄)</b>\n<pre>{content}</pre>")
        except Exception as e:
            send(f"❌ <code>{type(e).__name__}: {e}</code>")
        return True

    return False


def _handle_text_main(chat_id: str, text: str) -> None:
    text = text.strip()

    if text.lower() in ("/start", "/help", "도움말"):
        send_message(
            "🤖 <b>주식 분석 봇 (메인)</b>\n\n"
            "<b>종목 분석</b>\n"
            "▸ 종목명/코드 (예: <code>삼성전자</code>, <code>005930</code>)\n"
            "▸ <b>진입가</b>: <code>삼성전자 263000</code> → 익절/손절 지침\n"
            "▸ <b>분기실적</b>: <code>삼성전자 분기실적</code> → 매출/영업이익/FCF/ROE\n"
            "▸ <b>종토방</b>: <code>삼성전자 종토방</code> → 매수/매도 심리·공포/환희 지수\n"
            "▸ 부분 검색 시 후보 + 번호 선택\n"
            "▸ <i>결과는 Deepdive 봇 채널로 도착</i>\n\n"
            "<b>자연어 대화</b>\n"
            "▸ <i>PER이 뭐야?</i> / <i>지금 코스피 어때?</i>\n\n"
            "<b>설정 명령어</b>\n"
            "▸ <code>/schedule list</code> — 자동 일정\n"
            "▸ <code>/schedule us 07:00</code> — 시간 변경\n"
            "▸ <code>/run insight</code> — 즉시 실행\n"
            "▸ <code>/run pulse</code> — 지금 시장 급변(급락/급등) 원인 추적\n"
            "▸ <code>/model sonnet</code> — 모델 변경\n"
            "▸ <code>/logs deepdive</code> — 로그 마지막\n\n"
            "<b>자동 발송</b>\n"
            "▸ 평일 07:30 미국 / 20:00 한국 / 21:00 Deep-Dive\n"
            "▸ 매일 09:10 인사이트 / 09:20 차트"
        )
        return

    if text.startswith("/") and _handle_command(text):
        return

    if _try_pick_from_choices("main", chat_id, text, reply_mode=None):
        return

    if _try_resolve_or_list("main", chat_id, text, reply_mode=None):
        return

    # 자연어 fallback
    logger.info("[main] chat fallback for: %s", text[:60])
    send_message("💬 답변 생성 중... (10~30초)")
    try:
        send_message(claude_chat(text))
    except Exception as e:
        logger.exception("chat failed")
        send_message(f"⚠️ 답변 생성 실패: <code>{type(e).__name__}: {e}</code>")


# ── 핸들러: Deepdive 봇 (종목 매칭만) ────────────────────────────────
def _handle_text_consultant(chat_id: str, text: str) -> None:
    """Consultant 봇: 자유 대화 (주식 컨설턴트/펀드매니저 톤)."""
    text = text.strip()
    if text.lower() in ("/start", "/help", "도움말"):
        send_message(
            "💼 <b>주식 컨설턴트 봇</b>\n\n"
            "주식 컨설턴트 + 펀드매니저 역할로 답해드립니다.\n\n"
            "<b>예시 질문</b>\n"
            "▸ <i>PER이 낮은데 주가가 안 오르는 이유는?</i>\n"
            "▸ <i>현재 미국 시장 사이클은 어디쯤?</i>\n"
            "▸ <i>반도체 vs 2차전지, 6개월 관점에서 어디가 유리할까?</i>\n"
            "▸ <i>5,000만원 포트폴리오 어떻게 구성?</i>\n"
            "▸ <i>금리 인하 사이클 종료 시 영향은?</i>\n\n"
            "<b>자료 요청</b>\n"
            "▸ <i>현대차 최근 1개월 호악재 정리해줘</i>\n"
            "▸ <i>HBM 관련 글로벌 뉴스 요약</i>\n"
            "▸ <i>2026년 한국 배당주 시즌 정리</i>\n\n"
            "<i>매매 결정은 본인 책임. 컨설팅은 참고용입니다.</i>",
            mode="consultant",
        )
        return

    send_message("💼 컨설턴트가 답변 작성 중... (10~60초)", mode="consultant")
    try:
        reply = claude_chat(text, system_prompt=CONSULTANT_SYSTEM_PROMPT)
        send_message(reply, mode="consultant")
    except Exception as e:
        logger.exception("consultant chat failed")
        send_message(
            f"⚠️ 답변 생성 실패: <code>{type(e).__name__}: {e}</code>",
            mode="consultant",
        )


def _handle_text_note(chat_id: str, text: str) -> None:
    """Note 봇: 매수/매도 기록 + 시트/포지션 관련 명령 (/status /update /retarget)."""
    text = text.strip()
    if text.lower() in ("/start", "/help", "도움말"):
        send_message(
            "📝 <b>Note 봇 — 매매 기록 + 자동 지표</b>\n\n"
            "형식: <code>매수 종목 가격 이유</code>\n\n"
            "<b>예시</b>\n"
            "<code>매수 005930 263000 차트 정배열 회복</code>\n"
            "<code>매도 삼성전자 280000 1차 익절</code>\n"
            "<code>005930 매수 263000 외인 매수 + HBM 호재</code>\n\n"
            "<b>자동 처리</b>\n"
            "▸ PER, PBR, ROE(추정), RSI(14) 계산\n"
            "▸ 매수의 경우 1차 익절(+7%) / 2차 익절(+15%) / 손절(-5%) 가격 계산\n"
            "▸ 구글 시트에 자동 기록\n\n"
            "<b>명령어</b>\n"
            "▸ <code>/status</code> — 보유 포지션 평가손익 리포트\n"
            "▸ <code>/update 005930 tp1=320000 sl=295000</code> — 시트 값 직접 수정\n"
            "▸ <code>/retarget 005930</code> — TP/SL 재설정 (현재가 기준)\n"
            "▸ <code>/retarget 삼성전자 10 20 -3</code> — % 직접 지정",
            mode="note",
        )
        return

    # 슬래시 명령 디스패치: 허용 명령만 _handle_command로 위임 (응답은 Note 채널로)
    first = text.split(maxsplit=1)[0].lower() if text else ""
    if first in _NOTE_BOT_COMMANDS:
        _handle_command(text, reply_mode="note")
        return

    from .note_handler import handle_note
    reply = handle_note(text)
    send_message(reply, mode="note")


def _handle_text_monitor(chat_id: str, text: str) -> None:
    """모니터 봇: 포지션·시세 조회, TP/SL 체크/수정/재설정."""
    text = text.strip()
    if text.lower() in ("/start", "/help", "도움말"):
        send_message(
            "📡 <b>모니터 봇</b>\n\n"
            "<b>/status</b> — 보유 종목 즉시 평가손익 리포트\n"
            "<b>/check</b> — TP/SL 도달 즉시 체크 (장중 한정)\n"
            "<b>/update 005930 tp1=320000 sl=295000</b> — 시트 값 직접 수정\n"
            "<b>/retarget 005930</b> — TP/SL 재설정 (현재가 기준)\n"
            "<b>/retarget 삼성전자 10 20 -3</b> — % 직접 지정\n\n"
            "자동 일정:\n"
            "▸ 장중 09:00~15:30 매 30분 — TP/SL 도달 알림\n"
            "▸ 평일 16:00 — 일일 평가손익 리포트\n\n"
            "보유 종목은 Note 봇 시트의 잔여수량 > 0 종목 기준.",
            mode="monitor",
        )
        return
    if text.lower() == "/status":
        try:
            from . import monitor as mon
            mon.daily_report()
        except Exception as e:
            send_message(f"⚠️ <code>{type(e).__name__}: {e}</code>", mode="monitor")
        return
    if text.lower() == "/check":
        try:
            from . import monitor as mon
            mon.check_intraday()
            send_message("✅ 장중 체크 완료 (도달 종목이 있으면 별도 알림)", mode="monitor")
        except Exception as e:
            send_message(f"⚠️ <code>{type(e).__name__}: {e}</code>", mode="monitor")
        return
    # /update, /retarget은 _handle_command로 위임 (응답은 모니터 채널로)
    first = text.split(maxsplit=1)[0].lower() if text else ""
    if first in _MONITOR_BOT_COMMANDS:
        _handle_command(text, reply_mode="monitor")
        return
    send_message(
        "명령어: <code>/status</code>, <code>/check</code>, <code>/update</code>, <code>/retarget</code>, <code>/help</code>",
        mode="monitor",
    )


def _start_compare(chat_id: str, ticker_a: str, name_a: str, ticker_b: str, name_b: str) -> None:
    """두 종목 사이드바이사이드 비교 분석."""
    send_message(
        f"⚔️ <b>{name_a}</b> (<code>{ticker_a}</code>) vs <b>{name_b}</b> (<code>{ticker_b}</code>) 비교 분석 중... 약 1~2분",
        mode="kr_compare",
    )
    logger.info("Compare %s(%s) vs %s(%s) chat=%s", name_a, ticker_a, name_b, ticker_b, chat_id)
    try:
        reporter.run(
            "kr_compare",
            skip_weekend_check=True,
            forced_tickers=(ticker_a, ticker_b),
        )
    except Exception as e:
        logger.exception("compare failed")
        send_message(
            f"⚠️ 비교 분석 실패: <code>{type(e).__name__}: {e}</code>",
            mode="kr_compare",
        )


def _handle_text_deepdive(chat_id: str, text: str) -> None:
    text = text.strip()

    if text.lower() in ("/start", "/help", "도움말"):
        send_message(
            "🔍 <b>Deepdive 봇</b>\n\n"
            "<b>일반 분석</b>: <code>삼성전자</code>, <code>005930</code>\n"
            "<b>진입가 분석</b>: <code>삼성전자 263000</code> → 익절/손절 지침\n"
            "<b>분기실적</b>: <code>삼성전자 분기실적</code> → 매출/영업이익/FCF/ROE 비교\n"
            "<b>종토방</b>: <code>삼성전자 종토방</code> → 매수/매도 심리·공포/환희 지수\n"
            "<b>비교</b>: <code>삼성전자 vs SK하이닉스</code> → 사이드바이사이드 분석\n\n"
            "<b>명령어</b>\n"
            "▸ <code>/run deepdive</code> — 자동 deep-dive 즉시 실행 (결과는 이 채널로)\n\n"
            "부분 검색 (예: <code>현대</code>) 시 후보 + 번호 선택.\n"
            "분석은 약 1~2분 소요.",
            mode="kr_deepdive",
        )
        return

    # 슬래시 명령 디스패치: /run 등 (응답은 Deepdive 채널로)
    first = text.split(maxsplit=1)[0].lower() if text else ""
    if first in _DEEPDIVE_BOT_COMMANDS:
        _handle_command(text, reply_mode="kr_deepdive")
        return

    # 비교 패턴 우선 처리: "A vs B" / "A 대 B"
    compare = parse_compare_query(text)
    if compare:
        qa, qb = compare
        ra = resolve_ticker(qa)
        rb = resolve_ticker(qb)
        if not ra or not rb:
            missing = qa if not ra else qb
            send_message(
                f"❌ 비교용 종목은 정확한 이름/코드여야 합니다. '<code>{missing}</code>' 매칭 실패.",
                mode="kr_deepdive",
            )
            return
        _start_compare(chat_id, ra[0], ra[1], rb[0], rb[1])
        return

    if _try_pick_from_choices("deepdive", chat_id, text, reply_mode="kr_deepdive"):
        return

    if _try_resolve_or_list("deepdive", chat_id, text, reply_mode="kr_deepdive"):
        return

    send_message(
        f"❌ '<code>{text}</code>' 종목을 찾지 못했습니다.\n"
        "정확한 한국어 종목명 또는 6자리 코드를 보내주세요.",
        mode="kr_deepdive",
    )


# ── Polling 루프 ──────────────────────────────────────────────
def _poll_bot(label: str, token: str, expected_chat_id: str, handler: Callable[[str, str], None]) -> None:
    logger.info("[%s] Polling start. expected_chat=%s", label, expected_chat_id)
    api_base = f"https://api.telegram.org/bot{token}"
    offset = 0
    backoff = 1
    while True:
        try:
            r = requests.get(
                f"{api_base}/getUpdates",
                params={"offset": offset, "timeout": POLL_TIMEOUT, "allowed_updates": ["message"]},
                timeout=POLL_TIMEOUT + 10,
            )
            r.raise_for_status()
            data = r.json()
            backoff = 1
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    msg = upd.get("message") or upd.get("channel_post") or {}
                    chat = msg.get("chat") or {}
                    chat_id = str(chat.get("id") or "")
                    text = msg.get("text") or ""
                    if chat_id != str(expected_chat_id):
                        logger.info("[%s] ignoring chat %s", label, chat_id)
                        continue
                    if not text:
                        continue
                    threading.Thread(
                        target=handler, args=(chat_id, text), daemon=True
                    ).start()
                except Exception:
                    logger.exception("[%s] update handling crashed", label)
        except requests.exceptions.Timeout:
            # Long-poll natural timeout — perfectly normal, just re-poll immediately.
            backoff = 1
        except (requests.RequestException, requests.exceptions.JSONDecodeError) as e:
            logger.warning("[%s] poll error: %s (backoff %ds)", label, e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def run_poll_loop() -> None:
    config.assert_env()

    main_token = config.TELEGRAM_BOT_TOKEN
    main_chat = config.TELEGRAM_CHAT_ID
    logger.info("Main bot polling. chat=%s", main_chat)

    deepdive_token, deepdive_chat = config.get_credentials("kr_deepdive")
    deepdive_separate = (deepdive_token and deepdive_token != main_token)

    note_token, note_chat = config.get_credentials("note")
    note_separate = (note_token and note_token != main_token)

    consultant_token, consultant_chat = config.get_credentials("consultant")
    consultant_separate = (consultant_token and consultant_token != main_token)

    monitor_token, monitor_chat = config.get_credentials("monitor")
    monitor_separate = (monitor_token and monitor_token != main_token)

    threads: list[threading.Thread] = []

    main_t = threading.Thread(
        target=_poll_bot,
        args=("main", main_token, main_chat, _handle_text_main),
        daemon=True,
    )
    main_t.start()
    threads.append(main_t)

    if deepdive_separate:
        logger.info("Deepdive bot polling (separate). chat=%s", deepdive_chat)
        dd_t = threading.Thread(
            target=_poll_bot,
            args=("deepdive", deepdive_token, deepdive_chat, _handle_text_deepdive),
            daemon=True,
        )
        dd_t.start()
        threads.append(dd_t)

    if note_separate:
        logger.info("Note bot polling (separate). chat=%s", note_chat)
        note_t = threading.Thread(
            target=_poll_bot,
            args=("note", note_token, note_chat, _handle_text_note),
            daemon=True,
        )
        note_t.start()
        threads.append(note_t)

    if consultant_separate:
        logger.info("Consultant bot polling (separate). chat=%s", consultant_chat)
        c_t = threading.Thread(
            target=_poll_bot,
            args=("consultant", consultant_token, consultant_chat, _handle_text_consultant),
            daemon=True,
        )
        c_t.start()
        threads.append(c_t)

    if monitor_separate:
        logger.info("Monitor bot polling (separate). chat=%s", monitor_chat)
        m_t = threading.Thread(
            target=_poll_bot,
            args=("monitor", monitor_token, monitor_chat, _handle_text_monitor),
            daemon=True,
        )
        m_t.start()
        threads.append(m_t)

    # Block on main thread (KeepAlive will restart if it dies)
    main_t.join()


if __name__ == "__main__":
    _setup_logging()
    try:
        run_poll_loop()
    except KeyboardInterrupt:
        logger.info("interrupted")
