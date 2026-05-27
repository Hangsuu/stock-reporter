"""Render annotated price charts (PNG) for the daily chart lesson."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any

import FinanceDataReader as fdr
import matplotlib

matplotlib.use("Agg")  # headless

import matplotlib.pyplot as plt  # noqa: E402
import pytz  # noqa: E402
from matplotlib import font_manager  # noqa: E402

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

KOREAN_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "/System/Library/Fonts/Supplemental/NotoSansGothic-Regular.ttf",
]
for _f in KOREAN_FONT_CANDIDATES:
    if os.path.exists(_f):
        font_manager.fontManager.addfont(_f)
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=_f).get_name()
        break
plt.rcParams["axes.unicode_minus"] = False


def fetch_chart_data(code: str, days: int = 280) -> dict[str, Any]:
    end = datetime.now(KST).date()
    start = end - timedelta(days=days)
    df = fdr.DataReader(code, start, end).dropna()
    if df.empty:
        raise ValueError(f"No price data for {code}")

    df = df.copy()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA60"] = df["Close"].rolling(60).mean()
    df["MA200"] = df["Close"].rolling(200).mean()

    return {
        "code": code,
        "df": df,
        "current": float(df["Close"].iloc[-1]),
        "high_60d": float(df["Close"].tail(60).max()),
        "low_60d": float(df["Close"].tail(60).min()),
        "high_52w": float(df["Close"].max()),
        "low_52w": float(df["Close"].min()),
        "ma20": float(df["MA20"].iloc[-1]) if df["MA20"].notna().any() else None,
        "ma60": float(df["MA60"].iloc[-1]) if df["MA60"].notna().any() else None,
        "ma200": float(df["MA200"].iloc[-1]) if df["MA200"].notna().any() else None,
    }


def render_chart(
    code: str,
    name: str,
    chart_data: dict[str, Any] | None = None,
    *,
    annotations: dict[str, float | None] | None = None,
    output_path: str | None = None,
) -> str:
    """Save annotated chart, return file path.

    annotations keys (모두 옵션): entry, target, stop_loss
    """
    if chart_data is None:
        chart_data = fetch_chart_data(code)
    df = chart_data["df"]

    fig, ax = plt.subplots(figsize=(12, 7))
    ax.plot(df.index, df["Close"], label="종가", linewidth=1.6, color="#1f3a5f")

    if df["MA20"].notna().any():
        ax.plot(df.index, df["MA20"], label="MA20", linewidth=1.0, color="#ff7f0e", alpha=0.85)
    if df["MA60"].notna().any():
        ax.plot(df.index, df["MA60"], label="MA60", linewidth=1.0, color="#2ca02c", alpha=0.85)
    if df["MA200"].notna().any():
        ax.plot(df.index, df["MA200"], label="MA200", linewidth=1.0, color="#9467bd", alpha=0.7)

    ax.axhline(y=chart_data["high_60d"], color="gray", linestyle=":", alpha=0.5,
               label=f"60일고 {chart_data['high_60d']:,.0f}")
    ax.axhline(y=chart_data["low_60d"], color="gray", linestyle=":", alpha=0.5,
               label=f"60일저 {chart_data['low_60d']:,.0f}")

    ann = annotations or {}
    last_x = df.index[-1]
    if (entry := ann.get("entry")) is not None:
        ax.axhline(y=entry, color="#1f77b4", linestyle="--", linewidth=1.5, alpha=0.7)
        ax.annotate(f"진입 {entry:,.0f}", xy=(last_x, entry), xytext=(8, 0),
                    textcoords="offset points", color="#1f77b4", fontsize=9,
                    fontweight="bold", va="center")
    if (target := ann.get("target")) is not None:
        ax.axhline(y=target, color="#2ca02c", linestyle="--", linewidth=1.5, alpha=0.7)
        ax.annotate(f"목표 {target:,.0f}", xy=(last_x, target), xytext=(8, 0),
                    textcoords="offset points", color="#2ca02c", fontsize=9,
                    fontweight="bold", va="center")
    if (stop := ann.get("stop_loss")) is not None:
        ax.axhline(y=stop, color="#d62728", linestyle="--", linewidth=1.5, alpha=0.7)
        ax.annotate(f"손절 {stop:,.0f}", xy=(last_x, stop), xytext=(8, 0),
                    textcoords="offset points", color="#d62728", fontsize=9,
                    fontweight="bold", va="center")

    today = datetime.now(KST).strftime("%Y-%m-%d")
    ax.set_title(f"{name} ({code}) — {today}", fontsize=15, fontweight="bold", pad=12)
    ax.set_ylabel("주가 (원)", fontsize=10)
    ax.legend(loc="upper left", fontsize=9, framealpha=0.85)
    ax.grid(alpha=0.3, linestyle="-", linewidth=0.5)

    # x-axis date format
    fig.autofmt_xdate()
    fig.tight_layout()

    if output_path is None:
        out_dir = "/tmp"
        output_path = os.path.join(
            out_dir,
            f"chart_{code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
        )
    fig.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    logger.info("Chart saved: %s", output_path)
    return output_path
