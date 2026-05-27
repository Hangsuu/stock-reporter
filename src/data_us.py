"""US market snapshot via yfinance."""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

INDICES = {
    "^GSPC": "S&P 500",
    "^IXIC": "NASDAQ Composite",
    "^DJI": "Dow Jones",
    "^RUT": "Russell 2000",
    "^VIX": "VIX",
}

MAG7 = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "GOOGL": "Alphabet",
    "AMZN": "Amazon",
    "NVDA": "NVIDIA",
    "META": "Meta",
    "TSLA": "Tesla",
}

SECTOR_ETFS = {
    "XLK": "Technology",
    "XLF": "Financial",
    "XLV": "Health Care",
    "XLE": "Energy",
    "XLI": "Industrial",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLC": "Communication",
}

OTHERS = {
    "^TNX": "US 10Y Yield",
    "^IRX": "US 3M Yield",
    "GC=F": "Gold",
    "CL=F": "Crude Oil (WTI)",
    "DX-Y.NYB": "Dollar Index",
    "KRW=X": "USD/KRW",
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
}


def _row_to_quote(df: pd.DataFrame) -> dict[str, Any] | None:
    df = df.dropna(how="all")
    if df.empty:
        return None
    close = float(df["Close"].iloc[-1])
    prev = float(df["Close"].iloc[-2]) if len(df) >= 2 else close
    volume_raw = df["Volume"].iloc[-1] if "Volume" in df.columns else None
    volume = int(volume_raw) if pd.notna(volume_raw) else None
    return {
        "close": close,
        "prev_close": prev,
        "change": close - prev,
        "change_pct": ((close - prev) / prev * 100) if prev else 0.0,
        "volume": volume,
    }


def _bulk_quotes(tickers: list[str], period: str = "5d") -> dict[str, dict[str, Any] | None]:
    if not tickers:
        return {}

    data = yf.download(
        tickers=tickers,
        period=period,
        interval="1d",
        group_by="ticker",
        progress=False,
        threads=True,
        auto_adjust=False,
    )

    out: dict[str, dict[str, Any] | None] = {}
    for ticker in tickers:
        try:
            df = data if len(tickers) == 1 else data[ticker]
            out[ticker] = _row_to_quote(df)
        except Exception as e:
            logger.warning("yfinance bulk failed for %s: %s", ticker, e)
            out[ticker] = None

    # Fallback: per-ticker retry for any that came back empty (yfinance cache locks happen).
    for ticker, quote in list(out.items()):
        if quote is not None:
            continue
        try:
            df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
            out[ticker] = _row_to_quote(df)
        except Exception as e:
            logger.warning("yfinance retry failed for %s: %s", ticker, e)

    return out


def _decorate(mapping: dict[str, str], quotes: dict[str, dict | None]) -> list[dict]:
    return [
        {"ticker": t, "name": name, **q}
        for t, name in mapping.items()
        if (q := quotes.get(t))
    ]


def _bulk_technicals(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """52w high/low + MA50/MA200 from 1-year daily history."""
    if not tickers:
        return {}
    try:
        data = yf.download(
            tickers=tickers,
            period="1y",
            interval="1d",
            group_by="ticker",
            progress=False,
            threads=True,
            auto_adjust=False,
        )
    except Exception as e:
        logger.warning("bulk technicals download failed: %s", e)
        return {}

    out: dict[str, dict[str, Any]] = {}
    for ticker in tickers:
        try:
            df = data if len(tickers) == 1 else data[ticker]
            close = df["Close"].dropna()
            if close.empty:
                continue
            current = float(close.iloc[-1])
            high_52w = float(close.max())
            low_52w = float(close.min())
            ma50 = float(close.tail(50).mean()) if len(close) >= 20 else current
            ma200 = float(close.tail(200).mean()) if len(close) >= 60 else current
            out[ticker] = {
                "high_52w": high_52w,
                "low_52w": low_52w,
                "pct_from_52w_high": (current - high_52w) / high_52w * 100 if high_52w else 0.0,
                "pct_from_52w_low": (current - low_52w) / low_52w * 100 if low_52w else 0.0,
                "ma50": ma50,
                "ma200": ma200,
                "above_ma50_pct": (current - ma50) / ma50 * 100 if ma50 else 0.0,
                "above_ma200_pct": (current - ma200) / ma200 * 100 if ma200 else 0.0,
            }
        except Exception as e:
            logger.warning("technicals failed for %s: %s", ticker, e)
    return out


US_TOP20_GROUPS: dict[str, list[tuple[str, str]]] = {
    "big_tech": [
        ("AAPL", "Apple"),
        ("MSFT", "Microsoft"),
        ("NVDA", "NVIDIA"),
        ("GOOGL", "Alphabet"),
        ("AMZN", "Amazon"),
    ],
    "tech": [
        ("META", "Meta"),
        ("TSLA", "Tesla"),
        ("AVGO", "Broadcom"),
    ],
    "finance": [
        ("JPM", "JPMorgan Chase"),
    ],
    "consumer": [
        ("WMT", "Walmart"),
    ],
    "other_top20": [
        ("LLY", "Eli Lilly"),
        ("UNH", "UnitedHealth"),
        ("XOM", "ExxonMobil"),
        ("V", "Visa"),
        ("MA", "Mastercard"),
        ("BRK-B", "Berkshire Hathaway"),
        ("JNJ", "Johnson & Johnson"),
        ("PG", "Procter & Gamble"),
        ("COST", "Costco"),
        ("ORCL", "Oracle"),
    ],
}


def collect_us_top20_snapshot() -> dict[str, list[dict]]:
    """20 대표 종목 카테고리별 가격/등락률."""
    all_tickers = []
    for group in US_TOP20_GROUPS.values():
        all_tickers.extend([t for t, _ in group])
    quotes = _bulk_quotes(all_tickers)

    out: dict[str, list[dict]] = {}
    for group_name, members in US_TOP20_GROUPS.items():
        out[group_name] = [
            {"ticker": t, "name": name, **q}
            for t, name in members
            if (q := quotes.get(t))
        ]
    return out


def collect_yield_detail(ticker: str = "^TNX") -> dict[str, Any]:
    """Detailed metrics for a treasury yield (default: US 10Y).

    Yields are quoted in percent, so changes are reported in basis points (bp).
    """
    try:
        df = yf.Ticker(ticker).history(period="3mo", interval="1d").dropna()
    except Exception as e:
        logger.warning("yield_detail %s failed: %s", ticker, e)
        return {}
    if df.empty:
        return {}

    close = df["Close"]
    current = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) >= 2 else current
    week_ago = float(close.iloc[-6]) if len(close) >= 6 else current
    month_ago = float(close.iloc[-22]) if len(close) >= 22 else current
    high_3m = float(close.max())
    low_3m = float(close.min())

    return {
        "current_pct": current,
        "prev_pct": prev,
        "change_1d_bps": round((current - prev) * 100, 1),
        "change_5d_bps": round((current - week_ago) * 100, 1),
        "change_30d_bps": round((current - month_ago) * 100, 1),
        "high_3m_pct": high_3m,
        "low_3m_pct": low_3m,
        "vs_3m_high_bps": round((current - high_3m) * 100, 1),
        "vs_3m_low_bps": round((current - low_3m) * 100, 1),
    }


def collect_us_market_snapshot() -> dict[str, Any]:
    all_tickers = list(INDICES) + list(MAG7) + list(SECTOR_ETFS) + list(OTHERS)
    quotes = _bulk_quotes(all_tickers)

    # Technicals only on movers we actually analyze in depth.
    technicals = _bulk_technicals(list(MAG7) + list(SECTOR_ETFS))
    for ticker, tech in technicals.items():
        if quotes.get(ticker):
            quotes[ticker].update(tech)

    movers_pool = _decorate({**MAG7, **SECTOR_ETFS}, quotes)
    gainers = sorted(movers_pool, key=lambda x: x["change_pct"], reverse=True)[:5]
    losers = sorted(movers_pool, key=lambda x: x["change_pct"])[:5]

    return {
        "indices": _decorate(INDICES, quotes),
        "mag7": _decorate(MAG7, quotes),
        "sectors": _decorate(SECTOR_ETFS, quotes),
        "others": _decorate(OTHERS, quotes),
        "top_gainers": gainers,
        "top_losers": losers,
        "us_10y_detail": collect_yield_detail("^TNX"),
    }
