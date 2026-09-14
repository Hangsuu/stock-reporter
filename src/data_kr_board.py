"""네이버 종목 토론방 수집 (며칠치 게시글 + 추천/조회수).

2026-09-10 무렵부터 finance.naver.com/item/board.naver 가 stock.naver.com 새 토론방으로
302 리다이렉트된다. 옛 HTML 테이블 파싱은 에러 없이 0건을 돌려줬다. 그래서 새 토론방
화면이 직접 부르는 JSON API 두 개를 쓴다.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import pytz
import requests

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

POSTS_API = "https://stock.naver.com/api/community/discussion/posts"
# 목록 API의 recommendCount 는 항상 0이고 viewCount 는 아예 없다.
# 화면도 이 API로 조회·추천·비추천 수를 따로 받아 합친다.
REACTIONS_API = "https://stock.naver.com/api/community/discussion/posts/reactions"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-reporter)"}
PAGE_SIZE = 20  # 옛 게시판 한 페이지 크기. data_kr 의 게시 속도 계산이 'page1 = 최신 ~20건'을 전제한다.


def fetch_board_posts(code: str, pages: int = 8) -> list[dict[str, Any]]:
    """Fetch N pages of board posts (~20 per page = ~160 posts).

    Each post: {date 'YYYY.MM.DD HH:MM' (KST), title, writer, views, up, down}.
    Newest-first ordering preserved.
    """
    all_posts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    offset: str | None = None
    for page in range(1, pages + 1):
        params: dict[str, Any] = {
            "itemCode": code,
            "discussionType": "domesticStock",  # ETF 도 같은 값
            "isHolderOnly": "false",
            # '5% 이상 상승했어요' 같은 자동 소식 글 제외 — 게시 속도·심리는 사람이 쓴 글로만 본다
            "excludesItemNews": "true",
            "isItemNewsOnly": "false",
            "pageSize": PAGE_SIZE,
        }
        if offset is not None:
            params["offset"] = offset
        try:
            r = requests.get(POSTS_API, params=params, headers=HEADERS, timeout=10)
            r.raise_for_status()
            data = r.json()
            raw = data["posts"]
        except Exception as e:
            # 다음 페이지 커서(lastOffset)를 못 받았으니 더 넘길 수 없다.
            logger.warning("board page %s/%d failed: %s", code, page, e)
            break

        # 커서가 제자리면 앞 페이지 글이 다시 오므로 id로 거른다.
        fresh = [p for p in raw if str(p.get("id")) not in seen_ids]
        seen_ids.update(str(p.get("id")) for p in fresh)
        # 클린봇이 걸러낸 글은 화면에서도 제목·본문을 가린다.
        posts = [p for p in fresh if p.get("isCleanbotPassed") is not False]
        reactions = _fetch_reactions(code, page, [str(p.get("id")) for p in posts])
        for p in posts:
            title = (p.get("title") or "").strip()
            date_text = _to_board_date(p.get("writtenAt"))
            if not title or not date_text:
                continue
            rx = reactions.get(str(p.get("id")), {})
            all_posts.append({
                "date": date_text,
                "title": title,
                "writer": (p.get("writer") or {}).get("nickname") or "",
                "views": _count(rx.get("viewCount")),
                "up": _count(rx.get("recommendCount")),
                "down": _count(rx.get("notRecommendCount")),
            })

        offset = data.get("lastOffset")
        # 마지막 페이지면 posts=[] · lastOffset=null (없는 종목 코드도 같다).
        if not fresh or offset is None:
            break
        time.sleep(0.3)  # be nice to naver
    return all_posts


def _fetch_reactions(code: str, page: int, post_ids: list[str]) -> dict[str, dict[str, Any]]:
    """postId → {viewCount, recommendCount, notRecommendCount, ...}.

    실패하면 빈 dict — 제목·시각만으로도 심리·게시 속도는 볼 수 있어 글은 살리고 카운트만 0으로 둔다.
    """
    if not post_ids:
        return {}
    try:
        r = requests.get(
            REACTIONS_API,
            params={"postIds": ",".join(post_ids)},
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        return {str(x["postId"]): x for x in r.json()}
    except Exception as e:
        logger.warning("board reactions %s/%d failed (views/up/down=0): %s", code, page, e)
        return {}


def _to_board_date(written_at: str | None) -> str | None:
    """'2026-09-14T10:36:07' (KST, TZ 표기 없음) → '2026.09.14 10:36'.

    호출자들이 옛 게시판 형식 그대로 파싱·사전식 비교하므로 형식을 유지한다.
    """
    try:
        dt = datetime.fromisoformat(written_at)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(KST)
    return dt.strftime("%Y.%m.%d %H:%M")


def _count(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0
