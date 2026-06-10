"""투자 참고 지표 일일 스냅샷 (전일 대비 증감 포함)."""
from __future__ import annotations

import logging
from typing import Any

from .data_kr import collect_index_summary
from .data_us import _bulk_quotes

logger = logging.getLogger(__name__)

# Yahoo(yfinance)는 한국 지수 일봉을 간헐적으로 누락/지연시킨다
# (예: 2026-06-09 ^KS11 일봉 누락 → 장 시작 전 07:00 실행 시 06-08이 최신으로 잡혀 "이틀전" 표시).
# 한국 지수는 Naver 기반 collect_index_summary로 덮어써 최신 종가를 보장한다.
# 매핑: yfinance 티커 → collect_index_summary의 code
_KR_INDEX_YF_TO_CODE: dict[str, str] = {"^KS11": "KS11", "^KQ11": "KQ11"}

# (티커, 한글명, 카테고리, 단위)
MACRO_INDICATORS: list[tuple[str, str, str, str]] = [
    # 변동성·안전자산
    ("^VIX", "VIX 변동성지수", "변동성", ""),
    ("^MOVE", "MOVE 채권변동성", "변동성", ""),
    # 미국 금리
    ("^TNX", "미 10년물 국채금리", "금리", "%"),
    ("^IRX", "미 13주 단기금리", "금리", "%"),
    ("^FVX", "미 5년물 국채금리", "금리", "%"),
    # 환율
    ("DX-Y.NYB", "달러인덱스 DXY", "환율", ""),
    ("KRW=X", "USD/KRW 원달러", "환율", "원"),
    ("JPY=X", "USD/JPY 엔달러", "환율", ""),
    ("EURUSD=X", "EUR/USD 유로달러", "환율", ""),
    # 원자재
    ("CL=F", "WTI 원유", "원자재", "$"),
    ("BZ=F", "Brent 원유", "원자재", "$"),
    ("GC=F", "금 (Gold)", "원자재", "$"),
    ("SI=F", "은 (Silver)", "원자재", "$"),
    ("HG=F", "구리 (Copper)", "원자재", "$"),
    # 암호화폐
    ("BTC-USD", "비트코인", "코인", "$"),
    ("ETH-USD", "이더리움", "코인", "$"),
    # 미국 주요 지수 ETF
    ("SPY", "S&P 500 (SPY)", "미국지수", "$"),
    ("QQQ", "나스닥100 (QQQ)", "미국지수", "$"),
    ("DIA", "다우 (DIA)", "미국지수", "$"),
    ("IWM", "러셀2000 (IWM)", "미국지수", "$"),
    ("SOXX", "반도체 (SOXX)", "미국섹터", "$"),
    ("XLF", "금융 (XLF)", "미국섹터", "$"),
    ("XLE", "에너지 (XLE)", "미국섹터", "$"),
    # 한국 지수
    ("^KS11", "코스피", "한국지수", ""),
    ("^KQ11", "코스닥", "한국지수", ""),
    # 미국 채권 ETF (금리 inverse)
    ("TLT", "미 장기국채 ETF (TLT)", "채권", "$"),
    ("HYG", "하이일드채권 (HYG)", "채권", "$"),
]


def collect_macro_indicators() -> dict[str, Any]:
    """모든 매크로 지표의 전일 종가 + 변동률 dict 반환."""
    tickers = [t for t, _, _, _ in MACRO_INDICATORS]
    quotes = _bulk_quotes(tickers, period="5d")

    # 한국 지수는 Yahoo 누락/지연 회피를 위해 Naver(FDR) 데이터로 덮어쓴다.
    try:
        kr_by_code = {x["code"]: x for x in collect_index_summary()}
        for yf_ticker, code in _KR_INDEX_YF_TO_CODE.items():
            x = kr_by_code.get(code)
            if x:
                quotes[yf_ticker] = {
                    "close": x["close"],
                    "prev_close": x["prev_close"],
                    "change": x["change"],
                    "change_pct": x["change_pct"],
                    "volume": x["volume"],
                }
    except Exception as e:
        logger.warning("KR index override failed, keeping yfinance values: %s", e)

    rows: list[dict[str, Any]] = []
    by_category: dict[str, list[dict]] = {}

    for ticker, name, category, unit in MACRO_INDICATORS:
        q = quotes.get(ticker)
        if not q:
            logger.warning("Failed to fetch %s (%s)", ticker, name)
            continue
        row = {
            "ticker": ticker,
            "name": name,
            "category": category,
            "unit": unit,
            "close": q["close"],
            "prev_close": q["prev_close"],
            "change": q["change"],
            "change_pct": q["change_pct"],
        }
        rows.append(row)
        by_category.setdefault(category, []).append(row)

    return {
        "indicators": rows,
        "by_category": by_category,
        "count": len(rows),
    }
