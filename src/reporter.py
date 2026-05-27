"""Main entry: collect → analyze → send."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

import pytz

from . import config
from .analyst import analyze
from .data_kr import collect_kr_market_snapshot, collect_kr_sector_technicals, collect_kr_top20_snapshot
from .data_kr_fundamentals import (
    collect_candidate_pool,
    fetch_deep_data,
    fetch_fundamentals,
    filter_undervalued,
)
from .chart_renderer import fetch_chart_data, render_chart
from .data_us import collect_us_market_snapshot, collect_us_top20_snapshot
from .notifier import send_message, send_photo
from .picks_history import filter_recent, record_pick

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")


def _setup_logging() -> None:
    log_path = config.LOG_DIR / f"reporter_{datetime.now(KST).strftime('%Y%m')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _is_weekday() -> bool:
    return datetime.now(KST).weekday() < 5


_DAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


def _build_header(market: str, extra: str = "") -> str:
    now = datetime.now(KST)
    day_ko = _DAY_KO[now.weekday()]
    timestamp = now.strftime(f"%Y-%m-%d ({day_ko}) %H:%M KST")
    titles = {
        "us": "🇺🇸 <b>미국 시장 리포트</b>",
        "us_top20": "🌐 <b>미국 시총 탑20 동향</b>",
        "kr": "🇰🇷 <b>한국 시장 리포트</b>",
        "kr_top20": "🏯 <b>한국 시총 탑20 동향</b>",
        "kr_deepdive": "🔍 <b>한국 종목 Deep-Dive</b>",
        "kr_quarterly": "📊 <b>분기실적 분석</b>",
        "kr_board": "💬 <b>종토방 분위기</b>",
        "insight": "💡 <b>오늘의 투자 인사이트</b>",
        "chart_lesson": "📈 <b>오늘의 차트 강의</b>",
        "macro": "🌐 <b>향후 2주 매크로 캘린더</b>",
        "macro_daily": "📡 <b>매크로 지표 데일리</b>",
        "kr_compare": "⚔️ <b>한국 종목 비교 분석</b>",
    }
    title = titles.get(market, "📈 <b>리포트</b>")
    suffix = f" — {extra}" if extra else ""
    return f"{title}{suffix}\n📅 {timestamp}\n\n"


def _parse_annotations(text: str) -> tuple[str, dict[str, float]]:
    """Strip the trailing [ANNOTATIONS] line and parse key=value pairs.

    Robust to Claude wrapping the line in <code>...</code> tags.
    """
    import re

    annotations: dict[str, float] = {}
    m = re.search(r"\[ANNOTATIONS\]([^\n]*)", text)
    if not m:
        return text.rstrip(), annotations
    # Remove the entire line that contains [ANNOTATIONS] (incl. wrapping tags)
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    if line_end == -1:
        line_end = len(text)
    body = (text[:line_start] + text[line_end:]).rstrip()

    raw = re.sub(r"<[^>]+>", "", m.group(1))  # strip any HTML tags
    for kv in raw.split():
        if "=" in kv:
            k, v = kv.split("=", 1)
            try:
                annotations[k.strip()] = float(v.strip().replace(",", ""))
            except ValueError:
                pass
    return body, annotations


CHART_RECENT_DAYS = 14
DEEPDIVE_RECENT_DAYS = 30
INSIGHT_RECENT_DAYS = 7


def _pick_chart_subject() -> dict:
    """Pick a sufficiently-liquid large-cap, avoiding the last 14 days of picks."""
    import random

    pool = collect_candidate_pool(top_n=100)
    eligible = [
        p for p in pool
        if (p.get("volume") or 0) > 100_000
        and (p.get("market_cap") or 0) > 1_000_000_000_000
        and p.get("close")
    ]
    if not eligible:
        eligible = [p for p in pool if p.get("close")]
    if not eligible:
        raise RuntimeError("No eligible chart_lesson candidates")
    eligible = filter_recent(
        "chart_lesson", eligible, days=CHART_RECENT_DAYS,
        key=lambda c: c["ticker"],
    )
    return random.choice(eligible)


def _run_kr_board(*, forced_ticker: str | None, dry_run: bool) -> str | None:
    if not forced_ticker:
        raise ValueError("kr_board requires forced_ticker")
    ticker = forced_ticker
    from .data_kr_board import fetch_board_posts
    posts = fetch_board_posts(ticker, pages=8)
    fund = fetch_fundamentals(ticker) or {}
    name = fund.get("name") or ticker

    snapshot = {
        "code": ticker,
        "name": name,
        "posts": posts,
        "post_count": len(posts),
    }
    analysis = analyze("kr_board", snapshot)

    header = _build_header("kr_board", f"{name} ({ticker})")
    message = header + analysis

    if dry_run:
        logger.info("[DRY-RUN] kr_board (%d posts):\n%s", len(posts), message)
        return message

    send_message(message, mode="kr_deepdive")
    logger.info("kr_board sent for %s (posts=%d)", ticker, len(posts))
    return message


def _run_kr_quarterly(*, forced_ticker: str | None, dry_run: bool) -> str | None:
    if not forced_ticker:
        raise ValueError("kr_quarterly requires forced_ticker (분기실적은 사용자 종목 지정 필요)")
    ticker = forced_ticker
    deep = fetch_deep_data(ticker)
    if not deep.get("fundamentals"):
        deep["fundamentals"] = fetch_fundamentals(ticker) or {}

    name = (deep.get("fundamentals") or {}).get("name") or ticker
    analysis = analyze("kr_quarterly", deep)

    header = _build_header("kr_quarterly", f"{name} ({ticker})")
    message = header + analysis

    if dry_run:
        logger.info("[DRY-RUN] quarterly:\n%s", message)
        return message

    # Quarterly는 Deepdive 봇 채널과 같이 사용
    send_message(message, mode="kr_deepdive")
    logger.info("kr_quarterly sent for %s", ticker)
    return message


def _run_kr_deepdive(
    *,
    forced_ticker: str | None,
    dry_run: bool,
    user_note: str | None,
    user_entry_price: float | None = None,
) -> str | None:
    if forced_ticker:
        logger.info("Forced ticker: %s (skipping selection)", forced_ticker)
        ticker = forced_ticker
        deep = fetch_deep_data(ticker)
        if not deep.get("fundamentals"):
            deep["fundamentals"] = fetch_fundamentals(ticker) or {}
    else:
        ticker, deep = _select_and_fetch_deepdive()

    if user_entry_price:
        deep["user_entry_price"] = user_entry_price

    raw = analyze("kr_deepdive_report", deep, user_note=user_note)
    body, annotations = _parse_annotations(raw)

    name = (deep.get("fundamentals") or {}).get("name") or ticker

    # Render chart (entry/target/stop_loss lines if Claude provided them)
    try:
        chart_data = fetch_chart_data(ticker)
        chart_path = render_chart(ticker, name, chart_data, annotations=annotations)
    except Exception as e:
        logger.warning("chart render failed for %s: %s — sending text only", ticker, e)
        chart_path = None

    header = _build_header("kr_deepdive", f"{name} ({ticker})")
    full_message = header + body

    if dry_run:
        logger.info("[DRY-RUN] chart=%s annotations=%s", chart_path, annotations)
        logger.info("[DRY-RUN] body:\n%s", full_message)
        return full_message

    if chart_path:
        caption = f"🔍 <b>Deep-Dive</b> — {name} (<code>{ticker}</code>)"
        send_photo(chart_path, caption=caption, mode="kr_deepdive")
    send_message(full_message, mode="kr_deepdive")
    # Forced ticker calls bypass dedup history (user explicitly requested it)
    if not forced_ticker:
        record_pick("kr_deepdive", ticker, name)
    logger.info("kr_deepdive sent for %s", ticker)
    return full_message


def _run_chart_lesson(*, dry_run: bool, user_note: str | None) -> str | None:
    chosen = _pick_chart_subject()
    code, name = chosen["ticker"], chosen["name"]
    logger.info("Chart lesson subject: %s (%s)", name, code)

    chart_data = fetch_chart_data(code)
    snapshot = {
        "code": code,
        "name": name,
        "market": chosen.get("market"),
        "current": chart_data["current"],
        "recent_60d": [
            {"date": str(d.date()) if hasattr(d, "date") else str(d)[:10], "close": float(c)}
            for d, c in chart_data["df"]["Close"].tail(60).items()
        ],
        "ma": {
            "ma20": chart_data["ma20"],
            "ma60": chart_data["ma60"],
            "ma200": chart_data["ma200"],
        },
        "high_60d": chart_data["high_60d"],
        "low_60d": chart_data["low_60d"],
        "high_52w": chart_data["high_52w"],
        "low_52w": chart_data["low_52w"],
    }

    raw = analyze("chart_lesson", snapshot, user_note=user_note)
    body, annotations = _parse_annotations(raw)

    chart_path = render_chart(code, name, chart_data, annotations=annotations)

    if dry_run:
        logger.info("[DRY-RUN] chart=%s annotations=%s", chart_path, annotations)
        logger.info("[DRY-RUN] body:\n%s", body)
        return body

    caption = f"📈 <b>오늘의 차트 강의</b> — {name} (<code>{code}</code>)"
    send_photo(chart_path, caption=caption, mode="chart_lesson")
    send_message(body, mode="chart_lesson")
    record_pick("chart_lesson", code, name)
    logger.info("chart_lesson sent for %s", code)
    return body


def _select_and_fetch_deepdive() -> tuple[str, dict]:
    """후보 → Claude 선정 → deep data."""
    logger.info("Collecting KR candidate pool...")
    pool = collect_candidate_pool(top_n=300)
    candidates = filter_undervalued(pool)
    logger.info("Pool=%d, undervalued=%d", len(pool), len(candidates))
    if not candidates:
        raise RuntimeError("No undervalued candidates passed the filter today.")

    # 최근 N일 선정 종목 제외 (Claude가 모르므로 사전 필터링)
    candidates = filter_recent(
        "kr_deepdive", candidates, days=DEEPDIVE_RECENT_DAYS,
        key=lambda c: c["ticker"],
    )

    # Trim payload: send only the fields Claude needs to choose, not 60d arrays.
    slim = [
        {
            "ticker": c["ticker"],
            "name": c["name"],
            "industry_code": c.get("industry_code"),
            "market": c.get("market"),
            "close": c.get("close"),
            "change_pct": c.get("change_pct"),
            "per": c.get("per"),
            "forward_per": c.get("forward_per"),
            "pbr": c.get("pbr"),
            "eps": c.get("eps"),
            "bps": c.get("bps"),
            "div_yield": c.get("div_yield"),
            "foreign_ownership": c.get("foreign_ownership"),
            "high_52w": c.get("high_52w"),
            "low_52w": c.get("low_52w"),
            "market_cap": c.get("market_cap"),
            "volume": c.get("volume"),
        }
        for c in candidates
    ]

    selection_raw = analyze("kr_deepdive_select", {"candidates": slim})
    ticker = "".join(ch for ch in selection_raw if ch.isdigit())[:6]
    valid_tickers = {c["ticker"] for c in candidates}
    name_lookup = {c["ticker"]: c["name"] for c in candidates}

    # ticker가 candidates에 없으면 retry 또는 fallback
    if len(ticker) != 6 or ticker not in valid_tickers:
        logger.warning(
            "Selection returned invalid/unknown ticker %r (raw=%r). Trying retry...",
            ticker, selection_raw[:200],
        )
        retry_raw = analyze(
            "kr_deepdive_select",
            {"candidates": slim, "previous_invalid_attempt": ticker},
        )
        ticker = "".join(ch for ch in retry_raw if ch.isdigit())[:6]
        if ticker not in valid_tickers:
            # Final fallback: 후보 풀에서 결정론적으로 1개 (PER 최저값)
            import random
            fallback = min(
                candidates,
                key=lambda c: c.get("per") or 999,
            )
            ticker = fallback["ticker"]
            logger.warning("Retry also failed (raw=%r). Falling back to lowest-PER: %s", retry_raw[:200], ticker)

    logger.info("Selected: %s (%s)", ticker, name_lookup.get(ticker, "?"))

    deep = fetch_deep_data(ticker) or {}
    if not isinstance(deep, dict):
        deep = {}
    if not deep.get("fundamentals"):
        deep["fundamentals"] = fetch_fundamentals(ticker) or {}
    # 어느 필드라도 None이면 빈 dict/list로 (downstream .get 안전)
    for key, default in (
        ("fundamentals", {}),
        ("quarterly_finance", {}),
        ("annual_finance", {}),
        ("price_history_60d", []),
        ("technicals", {}),
        ("dart", {}),
        ("news", []),
    ):
        if deep.get(key) is None:
            deep[key] = default

    return ticker, deep


def run(
    market: str,
    dry_run: bool = False,
    skip_weekend_check: bool = False,
    forced_ticker: str | None = None,
    forced_tickers: tuple[str, str] | None = None,
    user_note: str | None = None,
    user_entry_price: float | None = None,
) -> str | None:
    config.assert_env()

    # insight·chart_lesson·macro는 주말에도 실행 (학습 콘텐츠 / 일요일 위클리)
    weekend_skipped_modes = ("insight", "chart_lesson", "macro")
    if config.WEEKDAY_ONLY and not skip_weekend_check and not _is_weekday() and market not in weekend_skipped_modes:
        logger.info("주말이라 스킵 (WEEKDAY_ONLY=true)")
        return None

    logger.info("Collecting %s market data...", market)
    header_extra = ""
    if market == "us":
        snapshot = collect_us_market_snapshot()
        kr_top = collect_kr_market_snapshot()
        snapshot["kr_context"] = {
            "indices": kr_top["indices"],
            "kospi_marketcap_top": kr_top["kospi_marketcap_top"][:8],
            "usd_krw": kr_top["usd_krw"],
            "sector_technicals": collect_kr_sector_technicals(),
        }
        analysis = analyze(market, snapshot, user_note=user_note)
    elif market == "us_top20":
        snapshot = collect_us_top20_snapshot()
        analysis = analyze(market, snapshot, user_note=user_note)
    elif market == "kr":
        snapshot = collect_kr_market_snapshot()
        analysis = analyze(market, snapshot, user_note=user_note)
    elif market == "kr_top20":
        snapshot = collect_kr_top20_snapshot()
        analysis = analyze(market, snapshot, user_note=user_note)
    elif market == "kr_deepdive":
        return _run_kr_deepdive(
            forced_ticker=forced_ticker,
            dry_run=dry_run,
            user_note=user_note,
            user_entry_price=user_entry_price,
        )
    elif market == "kr_quarterly":
        return _run_kr_quarterly(forced_ticker=forced_ticker, dry_run=dry_run)
    elif market == "kr_board":
        return _run_kr_board(forced_ticker=forced_ticker, dry_run=dry_run)
    elif market == "insight":
        from .picks_history import recent_tickers
        now = datetime.now(KST)
        snapshot = {
            "date": now.strftime("%Y-%m-%d"),
            "weekday_ko": _DAY_KO[now.weekday()],
            "weekday_en": now.strftime("%A"),
            "is_weekend": now.weekday() >= 5,
        }
        avoid = recent_tickers("insight", INSIGHT_RECENT_DAYS)
        effective_note = user_note or ""
        if avoid:
            avoid_str = ", ".join(sorted(avoid))
            dedup_hint = (
                f"\n[중복 회피] 최근 {INSIGHT_RECENT_DAYS}일 사용된 카테고리: {avoid_str}. "
                f"이번엔 이 카테고리들을 피해 다른 주제를 선택해주세요."
            )
            effective_note = (effective_note + dedup_hint).strip()
        raw = analyze("insight", snapshot, user_note=effective_note or None)

        # [TOPIC] 파싱 + 본문에서 제거
        import re as _re
        analysis = raw
        m = _re.search(r"\[TOPIC\]\s*category=([^\s\n]+)", raw)
        topic_category = None
        if m:
            topic_category = m.group(1).strip()
            line_start = raw.rfind("\n", 0, m.start()) + 1
            line_end = raw.find("\n", m.end())
            if line_end == -1:
                line_end = len(raw)
            analysis = (raw[:line_start] + raw[line_end:]).rstrip()
    elif market == "chart_lesson":
        return _run_chart_lesson(dry_run=dry_run, user_note=user_note)
    elif market == "macro_daily":
        from .data_macro import collect_macro_indicators
        snapshot = collect_macro_indicators()
        analysis = analyze("macro_daily", snapshot, user_note=user_note)
    elif market == "kr_compare":
        if not forced_tickers or len(forced_tickers) != 2:
            raise ValueError("kr_compare는 forced_tickers=(ticker_a, ticker_b) 필요")
        ta, tb = forced_tickers
        from .data_kr_fundamentals import fetch_deep_data, classify_stock
        data_a = fetch_deep_data(ta)
        data_b = fetch_deep_data(tb)
        if not data_a or not data_b:
            missing = ta if not data_a else tb
            raise RuntimeError(f"{missing} 데이터 조회 실패")
        snapshot = {
            "stock_a": data_a,
            "stock_b": data_b,
            "classification_a": classify_stock(data_a),
            "classification_b": classify_stock(data_b),
        }
        analysis = analyze("kr_compare", snapshot, user_note=user_note)
    elif market == "macro":
        from datetime import timedelta
        now = datetime.now(KST)
        # 2주치 윈도우 = 오늘부터 13일 후까지 (총 14일 포함)
        # 일요일 발송 시: 오늘(일) ~ 13일 후 토요일까지 자연스럽게 2주
        # 다른 요일 트리거: 오늘부터 2주
        window_start = now
        window_end = now + timedelta(days=13)
        snapshot = {
            "today": now.strftime("%Y-%m-%d (%A)"),
            "window_range": {
                "start": window_start.strftime("%Y-%m-%d"),
                "end": window_end.strftime("%Y-%m-%d"),
                "days": 14,
            },
            "now_kst": now.strftime("%Y-%m-%d %H:%M KST"),
            "hint": "향후 2주(14일) 미/한 매크로 캘린더 + 컨센서스 + 시장 영향 시나리오를 web_search로 조사. 이벤트 없는 날은 항목 자체 생략.",
        }
        analysis = analyze("macro", snapshot, user_note=user_note)
    else:
        raise ValueError(f"Unknown market: {market}")

    message = _build_header(market, header_extra) + analysis

    if dry_run:
        logger.info("[DRY-RUN] would send %d chars:\n%s", len(message), message)
        return message

    logger.info("Sending to Telegram (%d chars)...", len(message))
    send_message(message, mode=market)
    if market == "insight" and locals().get("topic_category"):
        record_pick("insight", locals()["topic_category"], "투자 인사이트")
    logger.info("Done.")
    return message


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a daily stock market report to Telegram.")
    parser.add_argument(
        "market",
        choices=["us", "us_top20", "kr", "kr_top20", "kr_deepdive", "kr_quarterly", "kr_board", "insight", "chart_lesson", "macro", "macro_daily"],
        help="us=미국, us_top20, kr=한국, kr_top20, kr_deepdive=종목 분석, kr_quarterly=분기실적, kr_board=종토방, insight, chart_lesson",
    )
    parser.add_argument("--dry-run", action="store_true", help="콘솔에만 출력, 텔레그램 전송 안 함")
    parser.add_argument("--force", action="store_true", help="주말이어도 실행")
    parser.add_argument("--ticker", help="kr_deepdive 모드에서 종목 강제 지정 (선정 스킵, 6자리)")
    parser.add_argument("--note", help="분석에 반영할 사용자 추가 요청/관점")
    args = parser.parse_args()

    _setup_logging()

    try:
        run(
            args.market,
            dry_run=args.dry_run,
            skip_weekend_check=args.force,
            forced_ticker=args.ticker,
            user_note=args.note,
        )
    except SystemExit:
        raise
    except Exception as e:
        logger.exception("FATAL: %s", e)
        if not args.dry_run:
            try:
                send_message(
                    f"⚠️ <b>{args.market.upper()} 리포트 실패</b>\n\n"
                    f"<code>{type(e).__name__}: {e}</code>\n\n"
                    f"로그: <code>{config.LOG_DIR}</code>"
                )
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
