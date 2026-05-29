"""모니터 봇: 보유 종목 TP/SL 체크 + 일일 평가손익 리포트.

장중 30분 (KST 09:00~15:30 평일): TP1/TP2/SL 도달 알림
마감 후 16:00 (평일): 보유 종목 평가손익 요약

알림 중복 방지: logs/monitor_alerts.json

TP/SL 가격 우선순위:
1. 시트의 1차익절/2차익절/손절 컬럼에서 절대가격 파싱 (사용자가 수정한 값 반영)
2. 파싱 실패 시 평단 × ±%로 fallback
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import FinanceDataReader as fdr
import pytz
import requests

from . import config
from .notifier import send_message

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")
ALERT_HISTORY_PATH = config.LOG_DIR / "monitor_alerts.json"

# TP/SL 비율 (note_handler와 동일)
TP1_PCT = 7.0
TP2_PCT = 15.0
SL_PCT = -5.0


def _setup_logging() -> None:
    log_path = config.LOG_DIR / f"monitor_{datetime.now(KST).strftime('%Y%m')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _is_kr_market_open() -> bool:
    """KST 평일 09:00~15:30 사이면 True."""
    now = datetime.now(KST)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 <= minutes <= 15 * 60 + 30


def _is_weekday() -> bool:
    return datetime.now(KST).weekday() < 5


def _normalize_ticker(raw) -> str:
    """구글 시트가 005930을 5930으로 자동 변환하는 문제 보정 — 6자리 zero-padding."""
    s = str(raw).strip()
    # 소수점 제거 (시트가 5930.0 같은 float로 줄 수도)
    if s.endswith(".0"):
        s = s[:-2]
    if s.isdigit() and 1 <= len(s) <= 6:
        return s.zfill(6)
    return s


# "281,500 (+7%)", "281500", "290,000" 등에서 첫 가격(정수)을 추출
_PRICE_EXTRACT_RE = re.compile(r"([\d,]+)")


def _parse_target_price(raw) -> float | None:
    """시트 셀에서 절대가격 추출. '281,500 (+7%)' → 281500. 비어있거나 0이면 None."""
    if raw is None or raw == "":
        return None
    # 시트가 숫자형으로 줄 수도
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v if v > 0 else None
    s = str(raw).strip()
    if not s:
        return None
    m = _PRICE_EXTRACT_RE.search(s)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
        return v if v > 0 else None
    except ValueError:
        return None


# 시트 datetime 파싱용 fallback 포맷.
# 실제 Apps Script doGet은 Date를 ISO 8601 UTC로 직렬화한다(예: 2026-05-28T23:38:06.000Z)
# → fromisoformat으로 우선 처리하고, 아래는 수동 입력/로케일 표시값 대비용 fallback.
_SHEET_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",     # 미국 로케일 표시값 (5/29/2026 8:38:06)
    "%m/%d/%Y %I:%M:%S %p",  # 5/29/2026 8:38:06 AM
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
)


def _parse_sheet_dt(raw: str) -> datetime:
    """시트 datetime 문자열을 naive-UTC datetime으로 파싱. 실패 시 datetime.min.

    정렬/비교 일관성을 위해 tz-aware 값은 UTC naive로 정규화한다(전 행이 UTC라 순서 보존).
    """
    s = (raw or "").strip()
    if not s:
        return datetime.min
    # ISO 8601 (Apps Script Date 직렬화). 3.11+ fromisoformat은 'Z'·밀리초를 처리한다.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    for fmt in _SHEET_DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    logger.warning("Unparseable sheet datetime: %r (treated as oldest)", s)
    return datetime.min


def _replay_avg_cost(txns: list[tuple[datetime, str, float, int]]) -> tuple[int, float]:
    """거래 내역을 시간순으로 재생해 (잔여수량, 이동평균 평단)을 계산한다.

    이동평균법: 매수 시 평단을 가중평균으로 갱신, 매도 시 평단은 유지하고 수량만 차감.
    잔량이 0 이하가 되면(완전 청산/과매도) 평단을 리셋해, 이후 재매수가
    새 평단으로 시작하도록 한다. 이렇게 해야 청산됐던 종목 재매수 시
    과거 매수분이 평단에 섞이지 않는다.

    txns의 첫 원소는 파싱된 datetime이며 이를 기준으로 안정 정렬한다.
    """
    qty = 0
    avg = 0.0
    for _dt, action, price, q in sorted(txns, key=lambda t: t[0]):
        if action == "매수":
            new_qty = qty + q
            avg = (qty * avg + price * q) / new_qty
            qty = new_qty
        elif action == "매도":
            qty -= q
            if qty <= 0:  # 완전 청산 → 다음 매수가 새 평단으로 시작
                qty = 0
                avg = 0.0
    return qty, avg


def fetch_positions() -> list[dict]:
    """Apps Script doGet으로 시트 전체 받아 ticker별 평단/잔여수량 계산.

    Returns: [{ticker, name, avg_price, quantity, last_buy_reason}] (잔여수량 > 0만)
    """
    if not config.NOTE_SHEET_URL:
        logger.warning("NOTE_SHEET_URL not configured")
        return []
    try:
        r = requests.get(config.NOTE_SHEET_URL, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("Sheet GET failed: %s", e)
        return []

    rows = data.get("rows") or []
    if not rows:
        return []

    # ticker별 매수/매도 집계
    agg: dict[str, dict] = {}
    for r in rows:
        ticker = _normalize_ticker(r.get("ticker") or "")
        if not ticker:
            continue
        action = str(r.get("action") or "").strip()
        try:
            price = float(r.get("price") or 0)
            qty = int(r.get("quantity") or 1)
        except (ValueError, TypeError):
            continue
        if price <= 0 or qty <= 0:
            continue
        slot = agg.setdefault(ticker, {
            "ticker": ticker,
            "name": str(r.get("name") or ticker),
            "txns": [],  # (datetime, action, price, qty) — 시간순 재생용
            "last_buy_reason": "",
            "last_action_dt": "",
            "tp1_price": None,
            "tp2_price": None,
            "sl_price": None,
            "last_buy_dt": datetime.min,
        })
        slot["name"] = str(r.get("name") or slot["name"])
        dt_raw = str(r.get("datetime") or "")
        pdt = _parse_sheet_dt(dt_raw)
        slot["txns"].append((pdt, action, price, qty))
        if action == "매수":
            slot["last_buy_reason"] = str(r.get("reason") or "")
            # 가장 최근 매수 행의 익절/손절가 보존 (사용자가 시트에서 수정한 값 우선).
            # 잔량>0인 포지션의 최근 매수는 항상 현재 보유 분이므로 전체 최근 매수 기준이 맞다.
            if pdt >= slot["last_buy_dt"]:
                slot["last_buy_dt"] = pdt
                slot["tp1_price"] = _parse_target_price(r.get("tp1"))
                slot["tp2_price"] = _parse_target_price(r.get("tp2"))
                slot["sl_price"] = _parse_target_price(r.get("sl"))
        slot["last_action_dt"] = dt_raw or slot["last_action_dt"]

    positions: list[dict] = []
    for ticker, s in agg.items():
        # 이동평균법으로 시간순 재생: 잔량이 0이 되면 평단을 리셋해
        # 청산 후 재매수 시 과거 매수분이 평단에 섞이지 않게 한다.
        remaining, avg_price = _replay_avg_cost(s["txns"])
        if remaining <= 0:
            continue
        # 시트 가격 우선, 없으면 평단 × % fallback
        tp1 = s["tp1_price"] if s["tp1_price"] else (avg_price * (1 + TP1_PCT / 100) if avg_price > 0 else None)
        tp2 = s["tp2_price"] if s["tp2_price"] else (avg_price * (1 + TP2_PCT / 100) if avg_price > 0 else None)
        sl = s["sl_price"] if s["sl_price"] else (avg_price * (1 + SL_PCT / 100) if avg_price > 0 else None)
        positions.append({
            "ticker": ticker,
            "name": s["name"],
            "avg_price": avg_price,
            "quantity": remaining,
            "last_buy_reason": s["last_buy_reason"],
            "last_action_dt": s["last_action_dt"],
            "tp1": tp1,
            "tp2": tp2,
            "sl": sl,
            "tp1_from_sheet": s["tp1_price"] is not None,
            "tp2_from_sheet": s["tp2_price"] is not None,
            "sl_from_sheet": s["sl_price"] is not None,
        })
    return positions


def _round_to_tick(price: float) -> int:
    """한국 호가 단위 반올림."""
    p = price
    if p < 1000:
        tick = 1
    elif p < 5000:
        tick = 5
    elif p < 10000:
        tick = 10
    elif p < 50000:
        tick = 50
    elif p < 100000:
        tick = 100
    elif p < 500000:
        tick = 500
    else:
        tick = 1000
    return int(round(p / tick) * tick)


def _format_target_display(price: float, avg_price: float | None) -> str:
    """'320,000 (+10.2%)' 형식. avg_price 모르면 가격만."""
    if avg_price is None or avg_price <= 0:
        return f"{int(price):,}"
    pct = (price - avg_price) / avg_price * 100
    sign = "+" if pct >= 0 else ""
    return f"{int(price):,} ({sign}{pct:.1f}%)"


def _find_position(ticker: str) -> dict | None:
    """ticker로 현재 보유 포지션 (avg_price/name) 조회. 없으면 None."""
    ticker = _normalize_ticker(ticker)
    for p in fetch_positions():
        if p["ticker"] == ticker:
            return p
    return None


def update_targets(
    ticker: str,
    tp1: float | None = None,
    tp2: float | None = None,
    sl: float | None = None,
) -> dict:
    """Apps Script에 update 요청 — 해당 ticker의 가장 최근 매수 행 셀 수정.

    시트에 저장되는 값은 평단 기준 '가격 (+%)' 포맷. 평단 조회 실패 시 가격만.
    Apps Script doPost가 mode='update'를 처리하도록 코드 업데이트가 선행되어야 함.
    """
    if not config.NOTE_SHEET_URL:
        raise RuntimeError("NOTE_SHEET_URL 미설정")
    ticker = _normalize_ticker(ticker)
    pos = _find_position(ticker)
    avg = pos["avg_price"] if pos else None
    name = pos["name"] if pos else ticker

    payload: dict = {"mode": "update", "ticker": ticker}
    if tp1 is not None:
        payload["tp1"] = _format_target_display(tp1, avg)
    if tp2 is not None:
        payload["tp2"] = _format_target_display(tp2, avg)
    if sl is not None:
        payload["sl"] = _format_target_display(sl, avg)
    r = requests.post(config.NOTE_SHEET_URL, json=payload, timeout=15)
    r.raise_for_status()
    try:
        result = r.json()
    except Exception:
        result = {"ok": False, "error": f"non-json response: {r.text[:200]}"}
    result["avg_price"] = avg
    result["name"] = name
    return result


def retarget(
    ticker: str,
    tp1_pct: float = 7.0,
    tp2_pct: float = 15.0,
    sl_pct: float = -5.0,
) -> dict:
    """현재가 기준으로 TP1/TP2/SL 자동 재계산 후 시트 수정."""
    ticker = _normalize_ticker(ticker)
    current = fetch_current_price(ticker)
    if current is None:
        raise RuntimeError(f"현재가 조회 실패: {ticker}")
    tp1 = _round_to_tick(current * (1 + tp1_pct / 100))
    tp2 = _round_to_tick(current * (1 + tp2_pct / 100))
    sl = _round_to_tick(current * (1 + sl_pct / 100))
    result = update_targets(ticker, tp1=tp1, tp2=tp2, sl=sl)
    result["current"] = current
    result["tp1"] = tp1
    result["tp2"] = tp2
    result["sl"] = sl
    result["pcts"] = (tp1_pct, tp2_pct, sl_pct)
    return result


def fetch_current_price(ticker: str) -> float | None:
    """FDR로 당일/직전 종가 조회."""
    ticker = _normalize_ticker(ticker)
    try:
        from datetime import timedelta
        end = datetime.now(KST).date()
        start = end - timedelta(days=10)
        df = fdr.DataReader(ticker, start, end).dropna()
        if df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as e:
        logger.warning("Current price fetch failed for %s: %s", ticker, e)
        return None


def _load_alerts() -> dict:
    if not ALERT_HISTORY_PATH.exists():
        return {}
    try:
        return json.loads(ALERT_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_alerts(data: dict) -> None:
    ALERT_HISTORY_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _alert_key(ticker: str, level: str) -> str:
    """levels: TP1, TP2, SL. 평단 변동 시 새로 알림하기 위해 day-of-month는 안 씀."""
    return f"{ticker}:{level}"


def check_intraday() -> None:
    """장중 30분 트리거: TP1/TP2/SL 도달 종목 알림. 중복 회피.

    가격 기준 = 시트의 익절가/손절가 컬럼 (사용자 수정 반영). 없으면 평단 ±% fallback.
    """
    _setup_logging()
    if not _is_kr_market_open():
        logger.info("Outside market hours (KST), skipping intraday check")
        return

    positions = fetch_positions()
    if not positions:
        logger.info("No active positions")
        return

    alerts = _load_alerts()
    notifications: list[str] = []

    for pos in positions:
        current = fetch_current_price(pos["ticker"])
        if current is None:
            continue
        avg = pos["avg_price"]
        if avg <= 0:
            continue
        pct = (current - avg) / avg * 100

        ticker = pos["ticker"]
        name = pos["name"]

        # 각 레벨: 시트 가격 우선, 평단 ±% fallback
        # alert 키에 가격을 박아 가격 변동(시트 수정/평단 변동) 시 자동 reset
        for level, target_price, base_pct, default_label in (
            ("TP2", pos["tp2"], TP2_PCT, "+15%"),
            ("TP1", pos["tp1"], TP1_PCT, "+7%"),
            ("SL", pos["sl"], SL_PCT, "-5%"),
        ):
            if target_price is None or target_price <= 0:
                continue
            key = _alert_key(ticker, level)
            prev = alerts.get(key)
            # 가격 1원 이상 차이 나면 history reset (시트 수정 or 평단 변동)
            if prev and abs(prev.get("price_at", 0) - target_price) > 1:
                prev = None
            already_hit = bool(prev and prev.get("hit"))
            condition = (current >= target_price) if base_pct > 0 else (current <= target_price)
            if condition and not already_hit:
                emoji = "🎯" if level.startswith("TP") else "⚠️"
                # 실제 도달가 vs 평단 대비 %도 표시
                tp_pct_from_avg = (target_price - avg) / avg * 100
                source = "시트" if pos.get(f"{level.lower()}_from_sheet") else "기본"
                label_map = {"TP1": "1차 익절선", "TP2": "2차 익절선", "SL": "손절선"}
                notifications.append(
                    f"{emoji} <b>{name}</b> (<code>{ticker}</code>) {label_map[level]} 도달\n"
                    f"   목표 <code>{int(target_price):,}원</code> ({source} · 평단比 {tp_pct_from_avg:+.1f}%)\n"
                    f"   평단 <code>{int(avg):,}원</code> · 현재 <code>{int(current):,}원</code> "
                    f"(<b>{pct:+.2f}%</b>, {pos['quantity']}주)"
                )
                alerts[key] = {"hit": True, "price_at": target_price, "ts": datetime.now(KST).isoformat()}
                # TP2 도달 시 TP1도 hit 처리 (TP1은 이미 지났을 테니 중복 알림 방지)
                if level == "TP2" and pos.get("tp1"):
                    alerts[_alert_key(ticker, "TP1")] = {
                        "hit": True, "price_at": pos["tp1"], "ts": datetime.now(KST).isoformat()
                    }

    if notifications:
        msg = "📡 <b>장중 도달 알림</b>\n\n" + "\n\n".join(notifications)
        send_message(msg, mode="monitor")
        _save_alerts(alerts)
        logger.info("Sent %d intraday alerts", len(notifications))
    else:
        logger.info("No threshold hits among %d positions", len(positions))


def daily_report() -> None:
    """마감 후 16:00: 보유 전종목 평가손익 + 평단/수량 요약."""
    _setup_logging()
    if not _is_weekday():
        logger.info("Weekend, skipping daily report")
        return

    positions = fetch_positions()
    if not positions:
        send_message(
            "📊 <b>일일 포지션 리포트</b>\n\n현재 보유 종목 없음. 노트 봇으로 매수 기록을 시작하세요.",
            mode="monitor",
        )
        return

    lines = ["📊 <b>일일 포지션 리포트</b>", f"📅 {datetime.now(KST).strftime('%Y-%m-%d (%a) %H:%M KST')}", ""]
    total_cost = 0.0
    total_value = 0.0
    rows: list[dict[str, Any]] = []

    for pos in positions:
        current = fetch_current_price(pos["ticker"])
        if current is None:
            rows.append({**pos, "current": None, "pnl_pct": None, "cost": pos["avg_price"] * pos["quantity"], "value": None})
            continue
        cost = pos["avg_price"] * pos["quantity"]
        value = current * pos["quantity"]
        pnl_pct = (current - pos["avg_price"]) / pos["avg_price"] * 100 if pos["avg_price"] > 0 else 0
        total_cost += cost
        total_value += value
        rows.append({**pos, "current": current, "pnl_pct": pnl_pct, "cost": cost, "value": value})

    # 정렬: 손익률 내림차순
    rows.sort(key=lambda r: (r.get("pnl_pct") or -999), reverse=True)

    # 헤더 요약
    if total_cost > 0:
        total_pnl_pct = (total_value - total_cost) / total_cost * 100
        total_pnl = total_value - total_cost
        sign = "📈" if total_pnl >= 0 else "📉"
        lines.append(f"{sign} <b>총 평가</b>: 매수 {int(total_cost):,}원 → 현재 {int(total_value):,}원")
        lines.append(f"   손익 <b>{int(total_pnl):+,}원</b> (<b>{total_pnl_pct:+.2f}%</b>) · {len(rows)}종목")
        lines.append("")

    # 종목별
    lines.append("<b>📋 종목별 상세</b>")
    for r in rows:
        ticker = r["ticker"]
        name = r["name"]
        avg = r["avg_price"]
        qty = r["quantity"]
        if r["current"] is None:
            lines.append(f"▸ <b>{name}</b> (<code>{ticker}</code>) {qty}주 평단 {int(avg):,}원 — 시세조회 실패")
            continue
        pnl_pct = r["pnl_pct"]
        current_price = r["current"]
        # 시트 가격 기준으로 zone 판정 (없으면 평단 ±%)
        tp2 = r.get("tp2")
        tp1 = r.get("tp1")
        sl = r.get("sl")
        zone = ""
        if tp2 and current_price >= tp2:
            zone = " · <b>🎯 2차 익절 도달</b>"
        elif tp1 and current_price >= tp1:
            zone = " · <b>🎯 1차 익절 도달</b>"
        elif sl and current_price <= sl:
            zone = " · <b>⚠️ 손절선 이탈</b>"
        # 도달 단계가 없으면 진행 정도 표시 (TP1까지 남은 % 또는 SL까지)
        emoji = "🟢" if pnl_pct >= 7 else ("🔵" if pnl_pct >= 0 else ("🟡" if pnl_pct >= -5 else "🔴"))
        target_line = ""
        if tp1 and tp2 and sl:
            target_line = f"   🎯 TP1 {int(tp1):,} · TP2 {int(tp2):,} · SL {int(sl):,}원"
        lines.append(
            f"{emoji} <b>{name}</b> (<code>{ticker}</code>) {qty}주\n"
            f"   평단 {int(avg):,}원 → 현재 <b>{int(current_price):,}원</b> "
            f"(<b>{pnl_pct:+.2f}%</b>){zone}" + (f"\n{target_line}" if target_line else "")
        )

    msg = "\n".join(lines)
    send_message(msg, mode="monitor")
    logger.info("Daily report sent: %d positions, total PnL %+.2f%%",
                len(rows), total_pnl_pct if total_cost > 0 else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Position monitor")
    parser.add_argument("mode", choices=["intraday", "daily"])
    parser.add_argument("--force", action="store_true", help="시간/주말 체크 무시")
    args = parser.parse_args()

    if args.mode == "intraday":
        if args.force:
            _setup_logging()
            check_intraday.__wrapped__ if False else None
            # bypass time check
            positions = fetch_positions()
            logger.info("Forced intraday check, %d positions", len(positions))
        check_intraday()
    elif args.mode == "daily":
        daily_report()


if __name__ == "__main__":
    main()
