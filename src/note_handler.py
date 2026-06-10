"""Note bot: 매수/매도 기록 + 지표 계산 + 구글 시트 기록.

사용자 입력 예:
  매수 005930 263000 5 차트 정배열 + HBM 호재
  매도 삼성전자 280000 3 1차 익절
  005930 매수 263000 외인 매수    (수량 생략 시 1주)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import FinanceDataReader as fdr
import pytz
import requests

from . import config
from .data_kr import get_krx_listing
from .data_kr_fundamentals import fetch_fundamentals

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

# 익절/손절 룰 (매수 가격 기준 %)
TAKE_PROFIT_1_PCT = 7.0
TAKE_PROFIT_2_PCT = 15.0
STOP_LOSS_PCT = -5.0

ACTION_KEYWORDS = {
    "매수": "buy", "buy": "buy", "샀음": "buy", "매입": "buy",
    "매도": "sell", "sell": "sell", "팔았음": "sell", "익절": "sell", "손절": "sell",
}

_PRICE_RE = re.compile(r"^(\d[\d,]*\.?\d*)(만원|만|원)?$")


def _parse_price(token: str) -> float | None:
    m = _PRICE_RE.match(token.strip())
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if (m.group(2) or "") in ("만", "만원"):
        v *= 10000
    return v if v >= 100 else None


_QTY_RE = re.compile(r"^\d+(?:주|x)?$", re.IGNORECASE)


def parse_note(text: str) -> dict | None:
    """Free-form parse.

    포맷: '<구분> <종목> <가격> [수량] [이유...]' (또는 종목/구분 순서 바꿈 허용)
    가격 다음 토큰이 순수 정수(또는 '5주', 'x5')면 수량으로 인식, 아니면 수량 1 + 그 토큰을 이유 시작으로.
    """
    parts = text.strip().split()
    if len(parts) < 3:
        return None

    if parts[0].lower() in ACTION_KEYWORDS:
        action_idx, ticker_idx, price_idx = 0, 1, 2
    elif parts[1].lower() in ACTION_KEYWORDS:
        ticker_idx, action_idx, price_idx = 0, 1, 2
    else:
        return None

    action = ACTION_KEYWORDS[parts[action_idx].lower()]
    action_kr = parts[action_idx]
    ticker_query = parts[ticker_idx]

    price = _parse_price(parts[price_idx])
    if price is None:
        return None

    # 가격 다음 토큰이 정수(또는 '5주'/'x5')면 수량, 아니면 reason 시작
    quantity = 1
    reason_start = price_idx + 1
    if len(parts) > reason_start:
        next_tok = parts[reason_start]
        m = _QTY_RE.match(next_tok)
        if m:
            digits = re.sub(r"[^\d]", "", next_tok)
            if digits:
                quantity = max(1, int(digits))
                reason_start += 1

    reason = " ".join(parts[reason_start:]).strip()

    return {
        "action": action,
        "action_kr": action_kr,
        "ticker_query": ticker_query,
        "price": price,
        "quantity": quantity,
        "reason": reason,
    }


def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas][-period:]
    losses = [-d if d < 0 else 0.0 for d in deltas][-period:]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _round_to_tick(price: float) -> int:
    """한국 주식 호가 단위 반올림."""
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


def calc_targets(action: str, entry_price: float) -> dict[str, Any]:
    """매수: TP1/TP2/SL 가격 + % 계산. 매도: 빈 dict."""
    if action != "buy":
        return {}
    return {
        "take_profit_1": _round_to_tick(entry_price * (1 + TAKE_PROFIT_1_PCT / 100)),
        "take_profit_2": _round_to_tick(entry_price * (1 + TAKE_PROFIT_2_PCT / 100)),
        "stop_loss": _round_to_tick(entry_price * (1 + STOP_LOSS_PCT / 100)),
        "take_profit_1_pct": TAKE_PROFIT_1_PCT,
        "take_profit_2_pct": TAKE_PROFIT_2_PCT,
        "stop_loss_pct": STOP_LOSS_PCT,
    }


def ai_review(action_kr: str, ticker: str, name: str, price: float, quantity: int,
              reason: str, metrics: dict, targets: dict) -> str:
    """Claude로 냉정한 매매 평가 1단락 생성 (시트에 들어감)."""
    import os
    import subprocess
    from .analyst import _claude_bin
    from . import config as cfg

    prompt = f"""당신은 냉정한 주식 평가자입니다. 다음 매매 기록에 대해 <b>한 단락 3~4문장, 300~500자</b>로 평가하세요.

# 기록
- 종목: {name} ({ticker})
- 구분: {action_kr}
- 가격: {int(price):,}원
- 수량: {quantity}주 (총 {int(price)*quantity:,}원)
- 이유: {reason or '(이유 미입력)'}

# 지표 (네이버 + RSI 14일)
- PER: {metrics.get('per')} / PBR: {metrics.get('pbr')}
- ROE(추정): {metrics.get('roe_est')}%
- RSI(14): {metrics.get('rsi_14')}
- 배당수익률: {metrics.get('dividend_yield')}%
- 외인소진율: {metrics.get('foreign_ownership')}%
- EPS: {metrics.get('eps')} / BPS: {metrics.get('bps')}

# 매수 시 자동 산출 익절/손절
- 1차 익절: {targets.get('take_profit_1_pct', '-')}%
- 2차 익절: {targets.get('take_profit_2_pct', '-')}%
- 손절: {targets.get('stop_loss_pct', '-')}%

# 평가 기준 (냉정하게)
1. 매매 이유의 객관성 — 데이터 기반인가, 감정적 결정인가
2. 지표가 시사하는 위험 — RSI 과매수/과매도, 고PER, 저ROE, 외인 이탈 등
3. 놓치고 있는 변수 — 매크로, 섹터 사이클, 산업 이슈
4. 단기 vs 장기 관점 적합성

# 출력 규칙 (필수, 엄격히 따를 것)
- 한국어 plain text (HTML 태그 X, 마크다운 X)
- 본문만 출력. 다음 절대 금지:
  · 인트로 ("OO 매수에 대한 평가입니다", "다음은 평가입니다", "이번 거래는…" 등)
  · 구분선 ("---", "===" 등)
  · 라벨 ("한줄평:", "결론:", "요약:", "총평:")
  · 끝 인사 ("이상", "참고하세요")
  · <b>마지막 문장에 종합/판결 톤 X</b>
    예시 금지: "이 거래의 성패를 가른다", "결국 X 여부가 핵심이다", "요약하면…",
              "한 줄로 말하면…", "결론적으로…", "전체적으로…"
- 평가 본문만 한 단락 (3~5문장, 300~500자)
- 모든 문장은 개별 분석 사실/의견으로만 (RSI 위험, PER 부담, 전략 합리성 등)
- 마지막 문장도 그저 한 가지 분석 포인트로 끝낸다 (요약 X)
- 단정적이지만 매매 권유 X
- 좋으면 좋다, 위험하면 위험하다 솔직히
"""

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    try:
        r = subprocess.run(
            [_claude_bin(), "-p", "--model", cfg.CLAUDE_MODEL or "opus"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        if r.returncode == 0 and r.stdout.strip():
            return _clean_review(r.stdout.strip())
        logger.warning("ai_review claude exit=%d stderr=%r", r.returncode, r.stderr[:200])
        return "(AI 리뷰 생성 실패)"
    except Exception as e:
        logger.warning("ai_review failed: %s", e)
        return "(AI 리뷰 생성 실패)"


_FORBIDDEN_LABEL_RE = re.compile(
    r"(?im)^\s*(한\s*줄\s*평|총\s*평|결\s*론|요\s*약|평\s*가|의\s*견|마\s*무\s*리|한마디)\s*[:：]\s*"
)
_FORBIDDEN_INTRO_RE = re.compile(
    r"(?m)^.*(?:매수에 대한 평가|매도에 대한 평가|다음은 평가|이번 거래는|평가합니다)[^.\n]*\.\s*\n"
)
_DIVIDER_RE = re.compile(r"(?m)^\s*[-=*#]{3,}\s*$\n?")


def _clean_review(text: str) -> str:
    """Strip forbidden patterns Claude sometimes outputs despite prompt."""
    # 1) 인트로 라인 제거
    text = _FORBIDDEN_INTRO_RE.sub("", text)
    # 2) 구분선 제거 (---, ===, ***)
    text = _DIVIDER_RE.sub("", text)
    # 3) "한줄평: X" 같은 라벨 prefix → 라벨만 제거하고 내용은 본문에 흡수
    #    예: "한줄평: 위험한 진입이다." → "위험한 진입이다."
    text = _FORBIDDEN_LABEL_RE.sub("", text)
    # 4) 라벨이 라인 끝에 단독으로 남아있으면 라인 통째 제거
    cleaned_lines = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            cleaned_lines.append(line)
            continue
        # "한줄평:" 만 있는 라인
        if re.match(r"^(한\s*줄\s*평|총\s*평|결\s*론|요\s*약|평\s*가|의\s*견)\s*[:：]?\s*$", s):
            continue
        cleaned_lines.append(line)
    text = "\n".join(cleaned_lines).strip()
    # 5) 연속 빈 줄 정리
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _resolve_ticker_for_note(query: str) -> tuple[str, str] | None:
    """간단 매핑: 6자리 코드 그대로 또는 정확 종목명."""
    q = query.strip()
    if q.isdigit() and len(q) == 6:
        try:
            df = get_krx_listing()
            row = df[df["Code"] == q]
            if not row.empty:
                return q, str(row.iloc[0]["Name"])
        except Exception:
            pass
        return q, q
    # 종목명 매칭
    try:
        df = get_krx_listing()
        row = df[df["Name"] == q]
        if not row.empty:
            return str(row.iloc[0]["Code"]), q
    except Exception:
        pass
    return None


def enrich_with_metrics(ticker: str) -> dict[str, Any]:
    """PER, PBR, ROE(추정), RSI 계산."""
    out: dict[str, Any] = {}

    fund = fetch_fundamentals(ticker) or {}
    out["per"] = fund.get("per")
    out["pbr"] = fund.get("pbr")
    out["eps"] = fund.get("eps")
    out["bps"] = fund.get("bps")
    # ROE는 네이버 fundamentals에 직접 없지만 EPS/BPS로 추정 가능
    if fund.get("eps") and fund.get("bps") and fund["bps"] > 0:
        out["roe_est"] = round(fund["eps"] / fund["bps"] * 100, 2)
    else:
        out["roe_est"] = None
    out["dividend_yield"] = fund.get("div_yield")
    out["foreign_ownership"] = fund.get("foreign_ownership")

    # RSI: 최근 30일 종가
    try:
        from datetime import timedelta
        end = datetime.now(KST).date()
        start = end - timedelta(days=60)
        df = fdr.DataReader(ticker, start, end).dropna()
        closes = df["Close"].tolist()
        out["rsi_14"] = calc_rsi(closes, period=14)
    except Exception as e:
        logger.warning("RSI calc failed for %s: %s", ticker, e)
        out["rsi_14"] = None

    return out


def append_to_sheet(row: dict) -> bool:
    """Google Apps Script 웹훅으로 POST."""
    if not config.NOTE_SHEET_URL:
        logger.warning("NOTE_SHEET_URL not configured")
        return False
    try:
        r = requests.post(config.NOTE_SHEET_URL, json=row, timeout=15)
        if r.status_code != 200:
            logger.warning("Sheet POST failed %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        logger.warning("Sheet POST exception: %s", e)
        return False


def handle_note(text: str) -> str:
    """Parse note + enrich + send to sheet. Return reply message."""
    parsed = parse_note(text)
    if not parsed:
        return (
            "❌ 형식 오류. 예시:\n"
            "<code>매수 005930 263000 차트 정배열</code>\n"
            "<code>삼성전자 매도 280000 1차 익절</code>"
        )

    resolved = _resolve_ticker_for_note(parsed["ticker_query"])
    if not resolved:
        return f"❌ '<code>{parsed['ticker_query']}</code>' 종목을 찾지 못했습니다."
    ticker, name = resolved

    metrics = enrich_with_metrics(ticker)
    targets = calc_targets(parsed["action"], parsed["price"])
    review = ai_review(
        parsed["action_kr"], ticker, name, parsed["price"],
        int(parsed.get("quantity", 1)), parsed["reason"],
        metrics, targets,
    )

    def _fmt_combined(price, pct):
        if price is None or pct is None:
            return None
        sign = "+" if pct >= 0 else ""
        return f"{int(price):,} ({sign}{pct:g}%)"

    now = datetime.now(KST)
    # 시트 컬럼 순서: 일시 / 구분 / 가격 / 수량 / 종목명 / 코드 / 이유 /
    #              1차익절 / 2차익절 / 손절 / 평단 / 잔여수량 / [지표] / AI 리뷰
    # 평단 / 잔여수량은 Apps Script가 그 행에 SUMIFS 수식을 자동 삽입함
    row = {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "action": parsed["action_kr"],
        "price": int(parsed["price"]),
        "quantity": int(parsed.get("quantity", 1)),
        "name": name,
        "ticker": ticker,
        "reason": parsed["reason"],
        # 새 키 (가격(+%) 한 문자열)
        "tp1_display": _fmt_combined(targets.get("take_profit_1"), targets.get("take_profit_1_pct")),
        "tp2_display": _fmt_combined(targets.get("take_profit_2"), targets.get("take_profit_2_pct")),
        "sl_display": _fmt_combined(targets.get("stop_loss"), targets.get("stop_loss_pct")),
        # 옛 키 (호환): 사용자가 Apps Script 옛 버전 그대로면 이게 사용됨
        "take_profit_1": targets.get("take_profit_1"),
        "take_profit_2": targets.get("take_profit_2"),
        "stop_loss": targets.get("stop_loss"),
        "take_profit_1_pct": targets.get("take_profit_1_pct"),
        "take_profit_2_pct": targets.get("take_profit_2_pct"),
        "stop_loss_pct": targets.get("stop_loss_pct"),
        "per": metrics.get("per"),
        "pbr": metrics.get("pbr"),
        "roe_est_pct": metrics.get("roe_est"),
        "rsi_14": metrics.get("rsi_14"),
        "eps": metrics.get("eps"),
        "bps": metrics.get("bps"),
        "dividend_yield_pct": metrics.get("dividend_yield"),
        "foreign_ownership_pct": metrics.get("foreign_ownership"),
        "ai_review": review,
    }

    sheet_ok = append_to_sheet(row)

    # 봇 응답
    qty = int(parsed.get("quantity", 1))
    total = int(parsed["price"]) * qty
    lines = [
        f"📝 <b>{parsed['action_kr']}</b> 기록 — {name} (<code>{ticker}</code>)",
        f"가격: <b>{int(parsed['price']):,}원</b> × {qty}주 = <b>{total:,}원</b>",
    ]
    if parsed["reason"]:
        lines.append(f"이유: {parsed['reason']}")

    lines.append("")
    lines.append("📊 <b>지표</b>")
    if metrics.get("per") is not None:
        lines.append(f"▸ PER {metrics['per']:.2f}배")
    if metrics.get("pbr") is not None:
        lines.append(f"▸ PBR {metrics['pbr']:.2f}배")
    if metrics.get("roe_est") is not None:
        lines.append(f"▸ ROE(추정) {metrics['roe_est']:.2f}%")
    if metrics.get("rsi_14") is not None:
        rsi = metrics["rsi_14"]
        zone = "과매수" if rsi >= 70 else "과매도" if rsi <= 30 else "중립"
        lines.append(f"▸ RSI(14) {rsi:.1f} ({zone})")

    if targets:
        lines.append("")
        lines.append("🎯 <b>익절/손절 (매수가 기준)</b>")
        lines.append(f"▸ 1차 익절: <code>{targets['take_profit_1']:,}원 (+{TAKE_PROFIT_1_PCT:.0f}%)</code>")
        lines.append(f"▸ 2차 익절: <code>{targets['take_profit_2']:,}원 (+{TAKE_PROFIT_2_PCT:.0f}%)</code>")
        lines.append(f"▸ 손절: <code>{targets['stop_loss']:,}원 ({STOP_LOSS_PCT:.0f}%)</code>")

    if review and review != "(AI 리뷰 생성 실패)":
        lines.append("")
        lines.append("🤖 <b>AI 냉정 평가</b>")
        # HTML 태그 충돌 방지: 리뷰는 plain이라 escape 그대로 OK
        lines.append(f"<i>{review[:600]}</i>")

    lines.append("")
    lines.append(f"📋 시트 기록: {'✅ 성공' if sheet_ok else '❌ 실패 (NOTE_SHEET_URL 확인)'}")

    return "\n".join(lines)
