"""네이버 종목 토론방 스크래핑 (며칠치 게시글 + 추천/조회수)."""
from __future__ import annotations

import logging
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BOARD_URL = "https://finance.naver.com/item/board.naver?code={code}&page={page}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-reporter)"}


def fetch_board_posts(code: str, pages: int = 8) -> list[dict[str, Any]]:
    """Fetch N pages of board posts (typically ~20 per page = ~160 posts).

    Each post: {date 'YYYY.MM.DD HH:MM', title, writer, views, up, down}.
    Newest-first ordering preserved.
    """
    all_posts: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        try:
            r = requests.get(
                BOARD_URL.format(code=code, page=page),
                headers=HEADERS,
                timeout=10,
            )
            soup = BeautifulSoup(r.text, "html.parser")
            table = soup.select_one("table.type2")
            if not table:
                continue
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) != 6:
                    continue
                title_a = tds[1].find("a")
                if not title_a:
                    continue
                title = title_a.get_text(strip=True)
                date_text = tds[0].get_text(strip=True)
                if not title or not date_text:
                    continue
                try:
                    views = int(tds[3].get_text(strip=True).replace(",", "") or 0)
                    up = int(tds[4].get_text(strip=True).replace(",", "") or 0)
                    down = int(tds[5].get_text(strip=True).replace(",", "") or 0)
                except ValueError:
                    views, up, down = 0, 0, 0
                all_posts.append({
                    "date": date_text,
                    "title": title,
                    "writer": tds[2].get_text(strip=True),
                    "views": views,
                    "up": up,
                    "down": down,
                })
            time.sleep(0.3)  # be nice to naver
        except Exception as e:
            logger.warning("board page %s/%d failed: %s", code, page, e)
    return all_posts
