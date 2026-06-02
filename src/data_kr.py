"""Korean market snapshot via FinanceDataReader.

pykrx requires KRX login since 2024 and returns empty responses without it,
so we use FinanceDataReader (Naver/Daum-backed) instead. No auth needed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import FinanceDataReader as fdr
import pandas as pd
import pytz
import yfinance as yf

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")


def _kst_date(days_ago: int = 0):
    return (datetime.now(KST) - timedelta(days=days_ago)).date()


def collect_index_summary() -> list[dict]:
    end = _kst_date(0)
    start = _kst_date(10)

    result = []
    for code, name in [("KS11", "KOSPI"), ("KQ11", "KOSDAQ")]:
        try:
            df = fdr.DataReader(code, start, end).dropna()
            if df.empty:
                continue
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) >= 2 else last
            close = float(last["Close"])
            prev_close = float(prev["Close"])
            volume = int(last["Volume"]) if "Volume" in df.columns and pd.notna(last["Volume"]) else 0
            trade_value = int(last["Amount"]) if "Amount" in df.columns and pd.notna(last["Amount"]) else 0
            result.append({
                "code": code,
                "name": name,
                "close": close,
                "prev_close": prev_close,
                "change": close - prev_close,
                "change_pct": (close - prev_close) / prev_close * 100 if prev_close else 0.0,
                "volume": volume,
                "trade_value": trade_value,
            })
        except Exception as e:
            logger.warning("index %s failed: %s", code, e)
    return result


_listing_cache: pd.DataFrame | None = None


def _krx_listing() -> pd.DataFrame:
    global _listing_cache
    if _listing_cache is None:
        _listing_cache = fdr.StockListing("KRX")
    return _listing_cache


def _by_market(market: str) -> pd.DataFrame:
    df = _krx_listing()
    return df[df["Market"] == market].copy()


def _row_to_quote(row: pd.Series) -> dict:
    return {
        "ticker": row["Code"],
        "name": row["Name"],
        "close": float(row["Close"]) if pd.notna(row["Close"]) else 0.0,
        "change_pct": float(row["ChagesRatio"]) if pd.notna(row["ChagesRatio"]) else 0.0,
        "volume": int(row["Volume"]) if pd.notna(row["Volume"]) else 0,
        "amount": int(row["Amount"]) if pd.notna(row["Amount"]) else 0,
        "market_cap": int(row["Marcap"]) if pd.notna(row["Marcap"]) else 0,
    }


def collect_top_marketcap(market: str = "KOSPI", n: int = 10) -> list[dict]:
    try:
        df = _by_market(market).dropna(subset=["Marcap"]).nlargest(n, "Marcap")
        return [_row_to_quote(row) for _, row in df.iterrows()]
    except Exception as e:
        logger.warning("top marketcap %s failed: %s", market, e)
        return []


def collect_top_movers(market: str = "KOSPI", n: int = 8) -> tuple[list[dict], list[dict]]:
    try:
        df = _by_market(market).dropna(subset=["ChagesRatio"])
        df = df[df["Volume"] > 0]
        gainers = df.nlargest(n, "ChagesRatio")
        losers = df.nsmallest(n, "ChagesRatio")
        return (
            [_row_to_quote(r) for _, r in gainers.iterrows()],
            [_row_to_quote(r) for _, r in losers.iterrows()],
        )
    except Exception as e:
        logger.warning("top movers %s failed: %s", market, e)
        return [], []


def collect_top_traded(market: str = "KOSPI", n: int = 10) -> list[dict]:
    try:
        df = _by_market(market).dropna(subset=["Amount"]).nlargest(n, "Amount")
        return [_row_to_quote(row) for _, row in df.iterrows()]
    except Exception as e:
        logger.warning("top traded %s failed: %s", market, e)
        return []


def _usd_krw() -> float | None:
    try:
        krw = yf.Ticker("KRW=X").history(period="5d")
        return float(krw["Close"].iloc[-1]) if not krw.empty else None
    except Exception:
        return None


# Sector representatives Korean retail investors compare against US movers.
# Used by US market report to suggest entry points after analyzing US-driven impact.
KR_SECTOR_TICKERS: dict[str, list[tuple[str, str]]] = {
    "반도체 (NVIDIA·AI 영향)": [
        ("005930", "삼성전자"),
        ("000660", "SK하이닉스"),
        ("042700", "한미반도체"),
    ],
    "2차전지 (Tesla·EV 영향)": [
        ("373220", "LG에너지솔루션"),
        ("006400", "삼성SDI"),
        ("247540", "에코프로비엠"),
    ],
    "자동차 (USD/KRW·관세)": [
        ("005380", "현대차"),
        ("000270", "기아"),
    ],
    "인터넷·IT (Mag7·빅테크)": [
        ("035420", "NAVER"),
        ("035720", "카카오"),
    ],
    "바이오 (Healthcare 섹터)": [
        ("207940", "삼성바이오로직스"),
    ],
    "방산 (Industrials·지정학)": [
        ("012450", "한화에어로스페이스"),
        ("047810", "한국항공우주"),
    ],
    "조선 (Industrials·해운)": [
        ("329180", "HD현대중공업"),
        ("042660", "한화오션"),
    ],
    "에너지·화학 (XLE·Crude Oil)": [
        ("096770", "SK이노베이션"),
        ("011170", "롯데케미칼"),
    ],
}


def collect_kr_sector_technicals() -> dict[str, list[dict]]:
    """For each sector, fetch 60d daily history → MA5/20/60, recent high/low.

    The US report uses this to recommend Korean entry points after analyzing
    US-driven impact (e.g. NVIDIA up → SK하이닉스 진입 타점).
    """
    end = _kst_date(0)
    start = _kst_date(120)  # 60 영업일 ≒ 120 캘린더일
    result: dict[str, list[dict]] = {}

    for sector, members in KR_SECTOR_TICKERS.items():
        sector_rows = []
        for code, name in members:
            try:
                df = fdr.DataReader(code, start, end).dropna()
                if df.empty:
                    continue
                close = df["Close"]
                last = float(close.iloc[-1])
                prev = float(close.iloc[-2]) if len(close) >= 2 else last
                row = {
                    "ticker": code,
                    "name": name,
                    "close": last,
                    "prev_close": prev,
                    "change_pct": (last - prev) / prev * 100 if prev else 0.0,
                    "ma5": float(close.tail(5).mean()) if len(close) >= 5 else last,
                    "ma20": float(close.tail(20).mean()) if len(close) >= 20 else last,
                    "ma60": float(close.tail(60).mean()) if len(close) >= 60 else last,
                    "high_60d": float(close.tail(60).max()),
                    "low_60d": float(close.tail(60).min()),
                    "volume": int(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0,
                }
                row["pct_from_60d_high"] = (last - row["high_60d"]) / row["high_60d"] * 100 if row["high_60d"] else 0.0
                row["pct_from_60d_low"] = (last - row["low_60d"]) / row["low_60d"] * 100 if row["low_60d"] else 0.0
                row["above_ma20_pct"] = (last - row["ma20"]) / row["ma20"] * 100 if row["ma20"] else 0.0
                row["above_ma60_pct"] = (last - row["ma60"]) / row["ma60"] * 100 if row["ma60"] else 0.0
                sector_rows.append(row)
            except Exception as e:
                logger.warning("KR technical %s failed: %s", code, e)
        if sector_rows:
            result[sector] = sector_rows
    return result


KR_TOP20_GROUPS: dict[str, list[tuple[str, str]]] = {
    "semiconductor": [
        ("005930", "삼성전자"),
        ("000660", "SK하이닉스"),
        ("042700", "한미반도체"),
    ],
    "battery": [
        ("373220", "LG에너지솔루션"),
        ("006400", "삼성SDI"),
        ("247540", "에코프로비엠"),
    ],
    "auto": [
        ("005380", "현대차"),
        ("000270", "기아"),
    ],
    "internet": [
        ("035420", "NAVER"),
        ("035720", "카카오"),
    ],
    "finance": [
        ("105560", "KB금융"),
        ("055550", "신한지주"),
    ],
    "bio": [
        ("207940", "삼성바이오로직스"),
        ("068270", "셀트리온"),
    ],
    "defense_ship": [
        ("012450", "한화에어로스페이스"),
        ("329180", "HD현대중공업"),
    ],
    "other_top20": [
        ("028260", "삼성물산"),
        ("051910", "LG화학"),
        ("005490", "POSCO홀딩스"),
        ("096770", "SK이노베이션"),
        ("034730", "SK"),
    ],
}


def collect_kr_top20_snapshot() -> dict[str, list[dict]]:
    """카테고리별 한국 시총 상위 20여 종목 시세."""
    from .data_kr_fundamentals import collect_candidate_pool, fetch_fundamentals
    pool = collect_candidate_pool(top_n=250)
    pool_by_code = {p["ticker"]: p for p in pool}

    out: dict[str, list[dict]] = {}
    for group, members in KR_TOP20_GROUPS.items():
        rows = []
        for code, name in members:
            p = pool_by_code.get(code)
            if p is None:
                # pool에 없으면 직접 조회 (예외 케이스)
                f = fetch_fundamentals(code)
                if not f:
                    continue
                p = f
            rows.append({
                "ticker": code,
                "name": name,
                "close": p.get("close"),
                "change_pct": p.get("change_pct"),
                "volume": p.get("volume"),
                "market_cap": p.get("market_cap"),
                "foreign_ownership": p.get("foreign_ownership"),
                "per": p.get("per"),
                "pbr": p.get("pbr"),
            })
        out[group] = rows
    return out


def collect_top_gainers_context(
    kospi_gainers: list[dict],
    kosdaq_gainers: list[dict],
    top_n: int = 3,
    news_max: int = 8,
    board_max_titles: int = 25,
    board_max_age_days: int = 2,
) -> list[dict[str, Any]]:
    """상위 N개 일간 상승 종목(KOSPI+KOSDAQ 합산)에 대해
    뉴스 헤드라인 + 종토방 최근 게시글 제목을 수집한다.

    상승의 *왜*를 구분 가능하게 하는 컨텍스트:
      - news: 펀더멘털·이벤트 촉매 (실적, 정책, 해외 이벤트 기대 등)
      - board_recent_titles: 군중심리/추측성 글의 분포·온도

    동일 종목이 두 시장에 중복 매칭되면 한 번만 잡고, change_pct 내림차순.
    종토방은 직전 board_max_age_days일 이내 게시글만(과거 정체 글 노이즈 회피).
    종목별 fetch는 ThreadPoolExecutor로 병렬화해 추가 지연을 ~2~3s로 억제.
    """
    # 1) 합치고 ticker dedup, change_pct 내림차순으로 상위 N
    combined: list[dict] = []
    seen: set[str] = set()
    for g in (kospi_gainers or []) + (kosdaq_gainers or []):
        code = g.get("ticker")
        if not code or code in seen:
            continue
        seen.add(code)
        combined.append(g)
    combined.sort(key=lambda x: x.get("change_pct") or 0.0, reverse=True)
    top = combined[:top_n]
    if not top:
        return []

    # 지연 import: data_kr_fundamentals / data_kr_board가 무거운 의존성을 끌고 와
    # 모듈 로드 시점부터 비용 발생하는 걸 피한다.
    from concurrent.futures import ThreadPoolExecutor

    from .data_kr_board import fetch_board_posts
    from .data_kr_fundamentals import fetch_naver_news

    cutoff = (datetime.now(KST) - timedelta(days=board_max_age_days)).strftime("%Y.%m.%d")

    def _enrich(g: dict) -> dict:
        code = g["ticker"]
        out: dict[str, Any] = {
            "ticker": code,
            "name": g.get("name"),
            "change_pct": g.get("change_pct"),
            "close": g.get("close"),
            "amount": g.get("amount"),
            "news": [],
            "board_recent_titles": [],
        }
        try:
            news = fetch_naver_news(code, max_items=news_max) or []
            out["news"] = [
                {
                    "title": n.get("title"),
                    "days_ago": n.get("days_ago"),
                    "office": n.get("office"),
                }
                for n in news[:news_max]
                if n.get("title")
            ]
        except Exception as e:
            logger.warning("news fetch failed for %s: %s", code, e)
        try:
            posts = fetch_board_posts(code, pages=1) or []
            # 'YYYY.MM.DD HH:MM' 사전식 비교로 최근 게시글만
            recent = [
                {
                    "date": p.get("date"),
                    "title": p.get("title"),
                    "views": p.get("views"),
                    "up": p.get("up"),
                }
                for p in posts
                if (p.get("date") or "") >= cutoff and p.get("title")
            ]
            out["board_recent_titles"] = recent[:board_max_titles]
        except Exception as e:
            logger.warning("board fetch failed for %s: %s", code, e)
        return out

    with ThreadPoolExecutor(max_workers=min(len(top), 4)) as pool:
        return list(pool.map(_enrich, top))


def collect_kr_market_snapshot() -> dict[str, Any]:
    kospi_g, kospi_l = collect_top_movers("KOSPI", 8)
    kosdaq_g, kosdaq_l = collect_top_movers("KOSDAQ", 8)
    return {
        "indices": collect_index_summary(),
        "kospi_marketcap_top": collect_top_marketcap("KOSPI", 10),
        "kosdaq_marketcap_top": collect_top_marketcap("KOSDAQ", 10),
        "kospi_gainers": kospi_g,
        "kospi_losers": kospi_l,
        "kosdaq_gainers": kosdaq_g,
        "kosdaq_losers": kosdaq_l,
        "kospi_top_traded": collect_top_traded("KOSPI", 10),
        "kosdaq_top_traded": collect_top_traded("KOSDAQ", 10),
        "usd_krw": _usd_krw(),
        "top_gainers_context": collect_top_gainers_context(kospi_g, kosdaq_g, top_n=3),
    }
