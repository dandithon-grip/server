"""F-16 별점 피드백 → 태그 가중치 (#50).

별점 1~5 를 받아 그 경로의 태그마다 (별점 − 3) × 0.1 을 더한다 (5점 +0.2, 1점 −0.2).
범위 −1.0~+1.0. User.tag_weights 에 저장하고 F-03 rank 가 clamp(Σ, ±2) × 10 으로 점수에 반영한다.
학습이 아니라 규칙이다 (발표 용어: 피드백 가중치).
"""

from collections import Counter
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import AppException, ErrorCode
from app.domain.feedback.models import (
    FEEDBACK_TAGS,
    HINT_MIN_LIKES,
    HINT_MIN_RATING,
    NEUTRAL_RATING,
    RATING_STEP,
    WEIGHT_MAX,
    WEIGHT_MIN,
    Feedback,
)
from app.domain.feedback.schemas import PreferenceHint
from app.domain.route.models import PlaceType, Route
from app.domain.route.scoring import TAG_RATIO_THRESHOLD
from app.domain.route.service import RouteService, get_route_service
from app.domain.user.models import User

LOCAL_TZ = ZoneInfo("Asia/Seoul")
HINT_MESSAGES = {
    PlaceType.RIVER: "요즘 하천길을 좋아하시네요",
    PlaceType.PARK: "요즘 공원길을 좋아하시네요",
    PlaceType.TREES: "요즘 가로수길을 좋아하시네요",
    PlaceType.SUNSET: "요즘 노을길을 좋아하시네요",
    PlaceType.BRIDGE: "요즘 다리 위 풍경을 좋아하시네요",
}


def route_feedback_tags(route: Route) -> list[PlaceType]:
    """가중치가 붙는 경로 태그. 저장된 정적 태그 + 일몰 방향 비율이 높으면 sunset."""
    tags = [PlaceType(value) for value in route.tags if PlaceType(value) in FEEDBACK_TAGS]
    if route.sunset_ratio >= TAG_RATIO_THRESHOLD and PlaceType.SUNSET not in tags:
        tags.append(PlaceType.SUNSET)
    return tags


def rating_delta(rating: int) -> float:
    """별점 하나가 태그 가중치에 더하는 값. 3점이 0 이다."""
    return round((rating - NEUTRAL_RATING) * RATING_STEP, 2)


def apply_delta(weights: dict[str, float], tags: list[PlaceType], delta: float) -> dict[str, float]:
    """태그마다 delta 를 더하고 ±1.0 으로 자른다. 새 dict 를 돌려준다 (JSONB 갱신용)."""
    updated = dict(weights)
    for tag in tags:
        value = updated.get(tag.value, 0.0) + delta
        updated[tag.value] = round(max(WEIGHT_MIN, min(WEIGHT_MAX, value)), 2)
    return updated


def top_tags(weights: dict[str, float], limit: int = 2) -> list[str]:
    """양수 가중치 상위 태그. LLM 입력(top_tag_weights)용."""
    positive = [(tag, w) for tag, w in weights.items() if w > 0]
    positive.sort(key=lambda item: item[1], reverse=True)
    return [tag for tag, _ in positive[:limit]]


class FeedbackService:
    def __init__(self, session: AsyncSession, route_service: RouteService) -> None:
        self._session = session
        self._routes = route_service

    async def give_feedback(self, user: User, route_id: UUID, rating: int) -> Feedback:
        """
        별점을 저장하고 사용자의 태그 가중치를 갱신한다.

        :param user: 로그인 사용자 (tag_weights 가 갱신된다)
        :param route_id: 내 동선의 경로
        :param rating: 1~5 (schemas 가 범위를 검증했다)
        :raises AppException: ROUTE_NOT_FOUND - 내 경로가 아니거나 없음
        """
        # 1. 내 경로인지 확인
        route = await self._routes.find_user_route(user.id, route_id)
        if route is None:
            raise AppException(ErrorCode.ROUTE_NOT_FOUND)
        today = datetime.now(LOCAL_TZ).date()
        tags = route_feedback_tags(route)

        # 2. 같은 날 같은 경로에 이미 줬으면 이전 별점과의 차이만 반영한다
        stmt = select(Feedback).where(
            Feedback.user_id == user.id, Feedback.route_id == route_id, Feedback.date == today
        )
        previous = (await self._session.execute(stmt)).scalar_one_or_none()
        old_delta = 0.0 if previous is None else rating_delta(previous.rating)
        delta = round(rating_delta(rating) - old_delta, 2)

        # 3. 피드백 upsert. 태그는 경로가 다시 만들어져도 남도록 함께 저장한다
        values = {"rating": rating, "tags": [tag.value for tag in tags]}
        upsert = insert(Feedback).values(user_id=user.id, route_id=route_id, date=today, **values)
        upsert = upsert.on_conflict_do_update(constraint="uq_feedback_user_route_date", set_=values)
        await self._session.execute(upsert)

        # 4. 가중치 갱신 (JSONB 는 새 dict 대입으로만 변경이 추적된다)
        if delta != 0:
            user.tag_weights = apply_delta(user.tag_weights, tags, delta)
        await self._session.commit()
        feedback = (await self._session.execute(stmt)).scalar_one()
        return feedback

    async def preference_hint(self, user: User) -> PreferenceHint | None:
        """같은 태그에 4점 이상 3회면 제안 문구. 기본 취향은 사용자가 바꿀 때만 바뀐다."""
        # 1. 4점 이상 평가의 저장된 태그를 센다 (경로가 지워져도 남아 있다)
        stmt = select(Feedback.tags).where(
            Feedback.user_id == user.id, Feedback.rating >= HINT_MIN_RATING
        )
        rows = (await self._session.execute(stmt)).scalars()
        counts = Counter(PlaceType(tag) for tags in rows for tag in tags)

        # 2. 3회 이상인 태그 중 최다
        eligible = [(tag, n) for tag, n in counts.items() if n >= HINT_MIN_LIKES]
        if not eligible:
            return None
        tag, _ = max(eligible, key=lambda item: item[1])
        return PreferenceHint(tag=tag.value, message=HINT_MESSAGES[tag])

    async def reset_weights(self, user: User) -> None:
        """가중치 초기화. 피드백 기록은 남긴다."""
        user.tag_weights = {}
        await self._session.commit()


def get_feedback_service(
    session: AsyncSession = Depends(get_db),
    route_service: RouteService = Depends(get_route_service),
) -> FeedbackService:
    return FeedbackService(session, route_service)
