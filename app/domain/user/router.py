from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.domain.user.models import User
from app.domain.user.schemas import UserResponse

router = APIRouter(prefix="/users", tags=["User"])


@router.get(
    "/me",
    response_model=UserResponse,
    summary="내 정보 조회",
    description="Supabase JWT 로 인증된 사용자의 정보를 반환한다. 최초 호출 시 사용자를 생성한다.",
    responses={401: {"description": "토큰 없음, 만료 또는 서명 불일치"}},
)
async def me(current_user: User = Depends(get_current_user)) -> UserResponse:
    return UserResponse.model_validate(current_user)
