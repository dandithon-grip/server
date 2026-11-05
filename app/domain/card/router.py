from fastapi import APIRouter, Depends, Query

from app.core.security import get_current_user
from app.domain.card.models import ReasonTag
from app.domain.card.schemas import TodayCardResponse
from app.domain.card.service import CardResult, CardService, get_card_service
from app.domain.commute.models import Preference
from app.domain.route.models import ActionType
from app.domain.route.schemas import to_route_response
from app.domain.user.models import User

router = APIRouter(prefix="/cards", tags=["Card"])


def _to_response(result: CardResult) -> TodayCardResponse:
    card = result.card
    return TodayCardResponse(
        date=card.date,
        preference=Preference(card.preference),
        action_type=ActionType(card.action_type),
        fallback_level=card.fallback_level,
        reason_tag=ReasonTag(card.reason_tag),
        text=card.text,
        deviation=card.deviation,
        by_llm=card.by_llm,
        sunset_at=card.sunset_at,
        sky=card.sky,
        air_grade=card.air_grade,
        route=to_route_response(result.chosen) if result.chosen else None,
        candidates=[to_route_response(r) for r in result.candidates],
        shortest=[to_route_response(r) for r in result.shortest],
    )


@router.get(
    "/today",
    response_model=TodayCardResponse,
    summary="오늘의 카드",
    description=(
        "후보 ≤3개와 오늘의 날씨·일몰을 LLM 에 넘겨 하나를 고르고 두 문장을 쓴다. "
        "하루 1장을 캐시한다. preference 를 주면 그 취향으로 다시 만들어 덮어쓴다 (취향 칩). "
        "LLM 실패 시 점수 1위 + 템플릿 문장."
    ),
    responses={
        401: {"description": "토큰 없음 또는 만료"},
        404: {"description": "동선이 없거나 경로를 아직 생성하지 않음"},
    },
)
async def today_card(
    preference: Preference | None = Query(
        None, description="취향 칩. 생략하면 캐시 또는 기본 취향"
    ),
    current_user: User = Depends(get_current_user),
    service: CardService = Depends(get_card_service),
) -> TodayCardResponse:
    result = await service.get_today(current_user, preference=preference)
    return _to_response(result)


@router.get(
    "/demo",
    response_model=TodayCardResponse,
    summary="둘러보기 데모 카드",
    description="토큰 없이 데모 계정의 오늘 카드를 돌려준다. 데모 시드가 돌아 있어야 한다.",
    responses={404: {"description": "데모 동선이 준비되지 않음"}},
)
async def demo_card(service: CardService = Depends(get_card_service)) -> TodayCardResponse:
    return _to_response(await service.get_demo())
