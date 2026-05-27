"""KR fundamentals via Naver mobile stock APIs.

We rely on Naver's mobile JSON endpoints because:
- pykrx requires KRX login since 2024 and returns empty otherwise.
- yfinance gives null trailingPE/priceToBook for many KR tickers.
The endpoints are stable and consumed by m.stock.naver.com itself.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

import FinanceDataReader as fdr
import pandas as pd
import pytz
import requests

from . import config

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

INTEGRATION_API = "https://m.stock.naver.com/api/stock/{code}/integration"
FINANCE_QUARTER_API = "https://m.stock.naver.com/api/stock/{code}/finance/quarter"
FINANCE_ANNUAL_API = "https://m.stock.naver.com/api/stock/{code}/finance/annual"
NEWS_API = "https://m.stock.naver.com/api/news/stock/{code}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-reporter)"}
TIMEOUT = 8


def _to_number(s: Any) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if not s or s in ("N/A", "-", "n/a"):
        return None
    cleaned = re.sub(r"[,배원%주]", "", s).strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_marketcap_korean(s: str | None) -> int | None:
    """'1,537조 5,713억' → 153,757,130,000,000"""
    if not s:
        return None
    s = s.replace(",", "").replace(" ", "")
    total = 0
    if m := re.search(r"(\d+)조", s):
        total += int(m.group(1)) * 10**12
    if m := re.search(r"(\d+)억", s):
        total += int(m.group(1)) * 10**8
    if total == 0 and s.replace(".", "", 1).isdigit():
        total = int(float(s))
    return total or None


def _get_json(url: str) -> dict | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        logger.debug("naver fetch failed %s: %s", url, e)
        return None


def _extract_integration(code: str, data: dict) -> dict[str, Any]:
    info = {item["key"]: item["value"] for item in data.get("totalInfos", [])}
    return {
        "ticker": code,
        "name": data.get("stockName"),
        "industry_code": data.get("industryCode"),
        "per": _to_number(info.get("PER")),
        "pbr": _to_number(info.get("PBR")),
        "eps": _to_number(info.get("EPS")),
        "bps": _to_number(info.get("BPS")),
        "forward_per": _to_number(info.get("추정PER")),
        "forward_eps": _to_number(info.get("추정EPS")),
        "div_yield": _to_number(info.get("배당수익률")),
        "div_per_share": _to_number(info.get("주당배당금")),
        "foreign_ownership": _to_number(info.get("외인소진율")),
        "high_52w": _to_number(info.get("52주 최고")),
        "low_52w": _to_number(info.get("52주 최저")),
        "open": _to_number(info.get("시가")),
        "high": _to_number(info.get("고가")),
        "low": _to_number(info.get("저가")),
        "prev_close": _to_number(info.get("전일")),
        "volume": _to_number(info.get("거래량")),
        "market_cap_str": info.get("시총"),
        "market_cap": _parse_marketcap_korean(info.get("시총")),
    }


def fetch_fundamentals(code: str) -> dict[str, Any] | None:
    data = _get_json(INTEGRATION_API.format(code=code))
    if not data:
        return None
    try:
        return _extract_integration(code, data)
    except Exception as e:
        logger.debug("parse %s failed: %s", code, e)
        return None


def collect_candidate_pool(top_n: int = 300, max_workers: int = 16) -> list[dict[str, Any]]:
    listing = fdr.StockListing("KRX")
    listing = listing[listing["Market"].isin(["KOSPI", "KOSDAQ"])]
    listing = listing.dropna(subset=["Marcap"]).nlargest(top_n, "Marcap")
    codes = listing["Code"].tolist()

    listing_by_code = listing.set_index("Code")
    results: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_fundamentals, c): c for c in codes}
        for f in as_completed(futures):
            code = futures[f]
            try:
                row = f.result()
            except Exception as e:
                logger.warning("worker %s failed: %s", code, e)
                continue
            if not row:
                continue
            extras = listing_by_code.loc[code]
            row["close"] = float(extras["Close"]) if pd.notna(extras["Close"]) else row.get("prev_close")
            row["change_pct"] = float(extras["ChagesRatio"]) if pd.notna(extras["ChagesRatio"]) else 0.0
            row["market"] = extras["Market"]
            if not row.get("market_cap"):
                row["market_cap"] = int(extras["Marcap"]) if pd.notna(extras["Marcap"]) else None
            results.append(row)
    return results


def filter_undervalued(
    pool: list[dict],
    *,
    per_range: tuple[float, float] = (1.0, 15.0),
    max_pbr: float = 2.0,
    min_marketcap: int = 500_000_000_000,
    min_volume: int = 30_000,
) -> list[dict]:
    """저PER, 저PBR, 일정 시총·유동성 종목만 추림."""
    out = []
    for p in pool:
        per, pbr = p.get("per"), p.get("pbr")
        if per is None or not (per_range[0] <= per <= per_range[1]):
            continue
        if pbr is None or not (0 < pbr < max_pbr):
            continue
        if (p.get("market_cap") or 0) < min_marketcap:
            continue
        if (p.get("volume") or 0) < min_volume:
            continue
        out.append(p)
    return out


def _extract_finance_rows(data: dict) -> dict[str, dict[str, str]]:
    """{ '매출액': {'2024.12.': '757,883', ...}, ...}"""
    fi = (data or {}).get("financeInfo", {})
    period_label = {p["key"]: p["title"] for p in fi.get("trTitleList", [])}
    out = {}
    for row in fi.get("rowList", []):
        title = row.get("title")
        if not title:
            continue
        out[title] = {
            period_label.get(k, k): (v.get("value") if isinstance(v, dict) else None)
            for k, v in row.get("columns", {}).items()
        }
    return out


def _days_ago_kst(yyyymmdd: str | None) -> int | None:
    if not yyyymmdd or len(yyyymmdd) < 8:
        return None
    try:
        d = datetime.strptime(yyyymmdd[:8], "%Y%m%d").date()
        return (datetime.now(KST).date() - d).days
    except ValueError:
        return None


def fetch_naver_news(code: str, max_items: int = 20) -> list[dict[str, Any]]:
    """Recent news headlines, sorted newest-first with days_ago metadata.

    Caller relies on days_ago to prioritize same-day news as the cause of
    today's price moves; older items are background only.
    """
    data = _get_json(NEWS_API.format(code=code))
    if not data or not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for group in data:
        for item in group.get("items", []) if isinstance(group, dict) else []:
            dt = item.get("datetime")
            days_ago = _days_ago_kst(dt)
            out.append({
                "datetime": dt,  # YYYYMMDDHHMM
                "days_ago": days_ago,  # 0=오늘, 1=어제, ...
                "is_today": days_ago == 0,
                "title": item.get("titleFull") or item.get("title"),
                "summary": (item.get("body") or "").strip()[:200],
                "office": item.get("officeName"),
                "url": item.get("mobileNewsUrl"),
            })

    # Newest first by datetime string (YYYYMMDDHHMM lexicographic order works)
    out.sort(key=lambda x: x.get("datetime") or "", reverse=True)
    return out[:max_items]


def fetch_dart_supplements(stock_code: str) -> dict[str, Any]:
    """DART supplemental data: company profile, latest full filing, recent disclosures.

    Returns {} when OPEN_DART_API_KEY isn't set or any error occurs (silent fallback).
    """
    if not config.OPEN_DART_API_KEY:
        return {}
    try:
        import OpenDartReader  # local import keeps the dependency optional
    except ImportError:
        logger.warning("OpenDartReader not installed; skipping DART supplements")
        return {}

    try:
        dart = OpenDartReader(config.OPEN_DART_API_KEY)
    except Exception as e:
        logger.warning("OpenDartReader init failed: %s", e)
        return {}

    out: dict[str, Any] = {}

    try:
        info = dart.company(stock_code)
        if info:
            out["company"] = {
                "corp_name": info.get("corp_name"),
                "ceo_nm": info.get("ceo_nm"),
                "est_dt": info.get("est_dt"),
                "acc_mt": info.get("acc_mt"),
                "induty_code": info.get("induty_code"),
                "corp_cls": info.get("corp_cls"),
                "hm_url": info.get("hm_url"),
            }
    except Exception as e:
        logger.warning("dart.company(%s): %s", stock_code, e)

    # Latest filed report: try this year then last year, in order of recency.
    year = datetime.now(KST).year
    REPRT_TRY = [("11011", "사업보고서"), ("11014", "3분기보고서"), ("11012", "반기보고서"), ("11013", "1분기보고서")]
    for try_year in (year, year - 1):
        for code, label in REPRT_TRY:
            try:
                fs = dart.finstate_all(stock_code, try_year, reprt_code=code)
            except Exception:
                continue
            if fs is None or fs.empty:
                continue
            cols = [c for c in ["account_nm", "sj_nm", "thstrm_amount", "frmtrm_amount", "frmtrm_q_amount"] if c in fs.columns]
            out["latest_filing"] = {
                "year": try_year,
                "reprt_code": code,
                "reprt_label": label,
                "rows": fs[cols].head(60).to_dict("records"),
            }
            break
        if "latest_filing" in out:
            break

    today = datetime.now(KST).date()
    past = today - timedelta(days=90)
    # A=정기공시(분기/반기/사업), B=주요사항보고서(자사주/합병/감자/증자), I=거래소공시(잠정실적/배당/IR/정정)
    KIND_LABELS = {"A": "정기", "B": "주요사항", "I": "거래소"}
    disclosures: list[dict] = []
    for kind, label in KIND_LABELS.items():
        try:
            d = dart.list(
                stock_code,
                start=past.strftime("%Y%m%d"),
                end=today.strftime("%Y%m%d"),
                kind=kind,
            )
        except Exception as e:
            logger.warning("dart.list(%s, kind=%s): %s", stock_code, kind, e)
            continue
        if d is None or d.empty:
            continue
        for _, row in d.iterrows():
            disclosures.append({
                "date": str(row.get("rcept_dt", "")),
                "kind": label,
                "report_nm": str(row.get("report_nm", "")).strip(),
            })
    disclosures.sort(key=lambda x: x["date"], reverse=True)
    for d in disclosures:
        d["days_ago"] = _days_ago_kst(d["date"])
        d["is_today"] = d["days_ago"] == 0
    if disclosures:
        out["disclosures"] = disclosures[:25]

    return out


PERSONA_CATALOG = {
    "Buffett": "경제적 해자, 장기 ROE, 단순 비즈니스, 우량주 매수 후 보유",
    "Graham": "안전마진, BPS 대비 가격 디스카운트, 부채비율, 청산가치",
    "Lynch": "PEG, 매출/이익 성장률, 일상 종목, 텐배거 잠재력, 턴어라운드",
    "Wood": "파괴적 혁신, 5년 후 시장 점유율, R&D 투자 강도",
    "Marks": "시장 사이클 위치, downside risk, 두 번째 단계 사고",
    "Dalio": "매크로 (금리·환율·원자재·신용 사이클), 산업 사이클",
    "Jones": "기술적 분석, MA·추세·변동성, 단기 진입 타점",
}

CATEGORY_TO_PERSONAS = {
    "value": ["Graham", "Buffett"],
    "growth": ["Lynch", "Wood"],
    "cyclical": ["Dalio"],
    "defensive": ["Buffett"],
    "turnaround": ["Lynch"],
}

ALWAYS_ON = ["Marks", "Jones"]  # 모든 종목 공통

# DART industry_code prefix → cyclical 여부 (한국표준산업분류 KSIC, 대분류 일부)
CYCLICAL_INDUSTRY_PREFIXES = {
    "B",   # 광업
    "C13", "C14", "C15",  # 섬유·의복·가죽
    "C19",  # 코크스·연탄·석유정제
    "C20",  # 화학물질
    "C24",  # 1차 금속
    "C25",  # 금속가공
    "C28",  # 전기장비 (일부)
    "C30",  # 자동차
    "C31",  # 기타 운송장비 (조선·항공)
    "F",   # 건설
    "K64", "K65", "K66",  # 금융·보험
}


def classify_stock(deep_data: dict[str, Any]) -> dict[str, Any]:
    """Tag a stock as value/growth/cyclical/defensive/turnaround and pick personas."""
    fund = deep_data.get("fundamentals") or {}
    quarterly = deep_data.get("quarterly_finance") or {}
    dart_company = (deep_data.get("dart") or {}).get("company") or {}

    per = fund.get("per")
    forward_per = fund.get("forward_per")
    pbr = fund.get("pbr")
    div_yield = fund.get("div_yield")
    eps = fund.get("eps")
    forward_eps = fund.get("forward_eps")

    categories: list[str] = []

    if per is not None and pbr is not None and 0 < per < 10 and pbr < 1.0:
        categories.append("value")

    if div_yield is not None and div_yield >= 3.0:
        categories.append("defensive")

    # 매출 성장률 (분기 5개 → 가장 오래된 vs 최신 비교, 거칠게 YoY 근사)
    sales = quarterly.get("매출액") or {}
    if len(sales) >= 4:
        try:
            sorted_periods = sorted(sales.keys())
            recent = float(str(sales[sorted_periods[-2]]).replace(",", ""))  # 직전 분기 실적 (마지막은 컨센일 수 있음)
            base_idx = max(0, len(sorted_periods) - 6)  # 4분기 전 ≈ YoY
            base = float(str(sales[sorted_periods[base_idx]]).replace(",", ""))
            if base > 0 and (recent / base - 1) > 0.15:
                categories.append("growth")
        except (ValueError, IndexError, TypeError):
            pass

    # 턴어라운드: 영업이익이 음수에서 양수로, 또는 4분기 연속 회복
    op = quarterly.get("영업이익") or {}
    if len(op) >= 3:
        try:
            sorted_periods = sorted(op.keys())
            vals = [float(str(op[p]).replace(",", "")) for p in sorted_periods if op[p]]
            if len(vals) >= 3 and vals[0] < 0 and vals[-1] > 0:
                categories.append("turnaround")
        except (ValueError, TypeError):
            pass

    # 시클리컬: DART 산업코드 prefix 매칭
    induty = str(dart_company.get("induty_code") or "")
    if induty:
        for prefix in CYCLICAL_INDUSTRY_PREFIXES:
            if induty.startswith(prefix.replace("C", "").replace("B", "").replace("F", "").replace("K", "")):
                # KSIC는 보통 숫자만이라 prefix 매칭이 까다로움. 단순화: 숫자 첫 두 자리.
                pass
        # 단순화된 매핑: 산업코드 두 자리만 봄
        sector_map = {
            "13": "cyclical", "14": "cyclical", "20": "cyclical",
            "24": "cyclical", "25": "cyclical", "28": "cyclical",
            "30": "cyclical", "31": "cyclical", "41": "cyclical",
            "42": "cyclical", "64": "cyclical", "65": "cyclical",
            "66": "cyclical",
        }
        if sector_map.get(induty[:2]) == "cyclical":
            categories.append("cyclical")

    if not categories:
        categories.append("defensive")  # 분류 실패 시 기본

    personas = set(ALWAYS_ON)
    for cat in categories:
        for p in CATEGORY_TO_PERSONAS.get(cat, []):
            personas.add(p)

    return {
        "categories": list(dict.fromkeys(categories)),
        "personas": sorted(personas),
    }


def fetch_deep_data(code: str) -> dict[str, Any]:
    """Selection 결과 종목에 대한 분기/연간 재무 + 60일 가격 + DART + 뉴스 보강."""
    fundamentals = fetch_fundamentals(code) or {}
    quarterly = _extract_finance_rows(_get_json(FINANCE_QUARTER_API.format(code=code)))
    annual = _extract_finance_rows(_get_json(FINANCE_ANNUAL_API.format(code=code)))
    dart_data = fetch_dart_supplements(code)
    news = fetch_naver_news(code, max_items=15)

    end = datetime.now(KST).date()
    start = end - timedelta(days=120)
    price_history: list[dict] = []
    try:
        df = fdr.DataReader(code, start, end).dropna()
        for date, row in df.tail(60).iterrows():
            price_history.append({
                "date": str(date.date()) if hasattr(date, "date") else str(date)[:10],
                "close": float(row["Close"]),
                "volume": int(row["Volume"]) if "Volume" in row else 0,
            })
    except Exception as e:
        logger.warning("price history %s failed: %s", code, e)

    closes = [p["close"] for p in price_history]
    technicals = {}
    if closes:
        last = closes[-1]
        technicals = {
            "ma5": sum(closes[-5:]) / min(5, len(closes)),
            "ma20": sum(closes[-20:]) / min(20, len(closes)),
            "ma60": sum(closes[-60:]) / min(60, len(closes)),
            "high_60d": max(closes),
            "low_60d": min(closes),
            "current": last,
        }

    payload = {
        "fundamentals": fundamentals,
        "quarterly_finance": quarterly,
        "annual_finance": annual,
        "price_history_60d": price_history,
        "technicals": technicals,
        "dart": dart_data,
        "news": news,
    }
    payload["classification"] = classify_stock(payload)
    return payload
