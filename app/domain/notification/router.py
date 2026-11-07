from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.domain.notification.schemas import (
    PushTokenResponse,
    RegisterPushTokenRequest,
    TestPushResponse,
)
from app.domain.notification.service import NotificationService, get_notification_service
from app.domain.user.models import User

router = APIRouter(prefix="/users/me/push-tokens", tags=["Notification"])


@router.put(
    "",
    response_model=PushTokenResponse,
    summary="퇴근 알림 기기 등록",
    description=(
        "브라우저가 알림 권한을 받은 뒤 Firebase 등록 토큰을 보낸다. 같은 토큰은 한 번만 저장되고, "
        "다른 계정으로 다시 등록하면 그 계정으로 옮긴다. 퇴근 10분 전에 오늘의 카드 알림이 간다."
    ),
    responses={401: {"description": "토큰 없음 또는 만료"}},
)
async def register_push_token(
    request: RegisterPushTokenRequest,
    current_user: User = Depends(get_current_user),
    service: NotificationService = Depends(get_notification_service),
) -> PushTokenResponse:
    await service.register(current_user.id, request.token)
    return PushTokenResponse(registered=True)


@router.delete(
    "",
    response_model=PushTokenResponse,
    summary="퇴근 알림 기기 해제",
    description="알림을 끄거나 로그아웃할 때 그 브라우저의 토큰을 지운다.",
    responses={401: {"description": "토큰 없음 또는 만료"}},
)
async def unregister_push_token(
    request: RegisterPushTokenRequest,
    current_user: User = Depends(get_current_user),
    service: NotificationService = Depends(get_notification_service),
) -> PushTokenResponse:
    await service.unregister(current_user.id, request.token)
    return PushTokenResponse(registered=False)


@router.post(
    "/test",
    response_model=TestPushResponse,
    summary="테스트 알림 보내기",
    description="내 기기 전부에 테스트 알림을 지금 보낸다. 시연 전 기기 확인용.",
    responses={
        401: {"description": "토큰 없음 또는 만료"},
        503: {"description": "서버에 FCM 키가 설정되지 않음"},
    },
)
async def send_test_push(
    current_user: User = Depends(get_current_user),
    service: NotificationService = Depends(get_notification_service),
) -> TestPushResponse:
    return TestPushResponse(sent=await service.send_test(current_user.id))
