from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.domain.feedback.schemas import (
    CreateFeedbackRequest,
    FeedbackResponse,
    TagWeightsResponse,
)
from app.domain.feedback.service import FeedbackService, get_feedback_service
from app.domain.user.models import User

feedback_router = APIRouter(prefix="/feedback", tags=["Feedback"])
tag_weights_router = APIRouter(prefix="/users/me/tag-weights", tags=["Feedback"])


@feedback_router.post(
    "",
    response_model=FeedbackResponse,
    summary="걷고 나서 별점 1~5",
    description=(
        "경로의 태그(river·park·trees·sunset·bridge)마다 (별점 − 3) × 0.1 을 더한다. "
        "5점 +0.2, 4점 +0.1, 3점 0, 2점 −0.1, 1점 −0.2. 범위 −1.0~+1.0. "
        "같은 날 같은 경로에 다시 주면 이전 별점과의 차이만 반영한다. "
        "가중치는 다음 샛길 점수에 반영된다."
    ),
    responses={
        401: {"description": "토큰 없음 또는 만료"},
        404: {"description": "내 동선의 경로가 아님"},
    },
)
async def give_feedback(
    request: CreateFeedbackRequest,
    current_user: User = Depends(get_current_user),
    service: FeedbackService = Depends(get_feedback_service),
) -> FeedbackResponse:
    feedback = await service.give_feedback(current_user, request.route_id, request.rating)
    return FeedbackResponse(
        route_id=request.route_id,
        date=feedback.date,
        rating=feedback.rating,
        weights=current_user.tag_weights,
    )


@tag_weights_router.get(
    "",
    response_model=TagWeightsResponse,
    summary="내 태그 가중치와 취향 제안",
    description=(
        "마이 › 취향 설정. 같은 태그에 4점 이상 평가가 3회 쌓이면 hint 에 제안 문구가 온다."
    ),
    responses={401: {"description": "토큰 없음 또는 만료"}},
)
async def get_tag_weights(
    current_user: User = Depends(get_current_user),
    service: FeedbackService = Depends(get_feedback_service),
) -> TagWeightsResponse:
    hint = await service.preference_hint(current_user)
    return TagWeightsResponse(weights=current_user.tag_weights, hint=hint)


@tag_weights_router.delete(
    "",
    response_model=TagWeightsResponse,
    summary="태그 가중치 초기화",
    description="👍/👎 로 쌓인 태그 가중치를 모두 0 으로 되돌린다. 피드백 기록은 남는다.",
    responses={401: {"description": "토큰 없음 또는 만료"}},
)
async def reset_tag_weights(
    current_user: User = Depends(get_current_user),
    service: FeedbackService = Depends(get_feedback_service),
) -> TagWeightsResponse:
    await service.reset_weights(current_user)
    return TagWeightsResponse(weights={}, hint=await service.preference_hint(current_user))
