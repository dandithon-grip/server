"""F-16 피드백 → 태그 가중치.

👍면 그 경로의 태그 각각 +0.2, 👎면 −0.2, 범위 −1.0~+1.0. User.tag_weights 에 저장하고
F-03 rank 가 clamp(Σ, ±2) × 10 으로 점수에 반영한다.
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
    HINT_MIN_UPS,
    WEIGHT_MAX,
    WEIGHT_MIN,
    WEIGHT_STEP,
    Feedback,
    Thumbs,
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


def apply_thumbs(
    weights: dict[str, float], tags: list[PlaceType], *, direction: int
) -> dict[str, float]:
    """태그마다 direction × 0.2 를 더하고 ±1.0 으로 자른다. 새 dict 를 돌려준다 (JSONB 갱신용)."""
    updated = dict(weights)
    for tag in tags:
        value = updated.get(tag.value, 0.0) + direction * WEIGHT_STEP
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

    async def give_feedback(self, user: User, route_id: UUID, thumbs: Thumbs) -> Feedback:
        """
        👍/👎 를 저장하고 사용자의 태그 가중치를 갱신한다.

        :param user: 로그인 사용자 (tag_weights 가 갱신된다)
        :param route_id: 내 동선의 경로
        :raises AppException: ROUTE_NOT_FOUND - 내 경로가 아니거나 없음
        """
        # 1. 내 경로인지 확인
        route = await self._routes.find_user_route(user.id, route_id)
        if route is None:
            raise AppException(ErrorCode.ROUTE_NOT_FOUND)
        today = datetime.now(LOCAL_TZ).date()

        # 2. 같은 날 같은 경로에 이미 눌렀으면 그 효과를 되돌릴 방향을 계산한다
        stmt = select(Feedback).where(
            Feedback.user_id == user.id, Feedback.route_id == route_id, Feedback.date == today
        )
        previous = (await self._session.execute(stmt)).scalar_one_or_none()
        new_sign = 1 if thumbs == Thumbs.UP else -1
        old_sign = 0 if previous is None else (1 if previous.thumbs == Thumbs.UP.value else -1)
        direction = new_sign - old_sign  # 처음이면 ±1, 바꾸면 ±2, 같으면 0

        # 3. 피드백 upsert
        upsert = insert(Feedback).values(
            user_id=user.id, route_id=route_id, date=today, thumbs=thumbs.value
        )
        upsert = upsert.on_conflict_do_update(
            constraint="uq_feedback_user_route_date", set_={"thumbs": thumbs.value}
        )
        await self._session.execute(upsert)

        # 4. 가중치 갱신 (JSONB 는 새 dict 대입으로만 변경이 추적된다)
        if direction != 0:
            user.tag_weights = apply_thumbs(
                user.tag_weights, route_feedback_tags(route), direction=direction
            )
        await self._session.commit()
        feedback = (await self._session.execute(stmt)).scalar_one()
        return feedback

    async def preference_hint(self, user: User) -> PreferenceHint | None:
        """같은 태그 👍 3회 누적이면 제안 문구. 기본 취향은 사용자가 바꿀 때만 바뀐다."""
        # 1. 👍 피드백의 경로 태그를 센다
        stmt = select(Feedback.route_id).where(
            Feedback.user_id == user.id, Feedback.thumbs == Thumbs.UP.value
        )
        route_ids = list((await self._session.execute(stmt)).scalars())
        routes = await self._routes.list_routes_by_ids(route_ids)
        counts = Counter(tag for route in routes for tag in route_feedback_tags(route))

        # 2. 3회 이상인 태그 중 최다
        eligible = [(tag, n) for tag, n in counts.items() if n >= HINT_MIN_UPS]
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
