from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.domain.feedback.models import Thumbs
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
    summary="걷고 나서 👍/👎",
    description=(
        "경로의 태그(river·park·trees·sunset·bridge)마다 👍 +0.2 / 👎 −0.2, 범위 −1.0~+1.0. "
        "같은 날 같은 경로에 다시 누르면 덮어쓴다. 가중치는 다음 샛길 점수에 반영된다."
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
    feedback = await service.give_feedback(current_user, request.route_id, request.thumbs)
    return FeedbackResponse(
        route_id=feedback.route_id,
        date=feedback.date,
        thumbs=Thumbs(feedback.thumbs),
        weights=current_user.tag_weights,
    )


@tag_weights_router.get(
    "",
    response_model=TagWeightsResponse,
    summary="내 태그 가중치와 취향 제안",
    description="마이 › 취향 설정. 같은 태그 👍 3회 누적이면 hint 에 제안 문구가 온다.",
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
    responses={401: {"description": "토큰 없음 또는 만료"}},
)
async def reset_tag_weights(
    current_user: User = Depends(get_current_user),
    service: FeedbackService = Depends(get_feedback_service),
) -> TagWeightsResponse:
    await service.reset_weights(current_user)
    return TagWeightsResponse(weights={}, hint=await service.preference_hint(current_user))
