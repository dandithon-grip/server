from pydantic import BaseModel, ConfigDict, Field

from app.domain.notification.models import PUSH_TOKEN_MAX_LENGTH


class RegisterPushTokenRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    token: str = Field(
        ...,
        min_length=1,
        max_length=PUSH_TOKEN_MAX_LENGTH,
        description="프론트가 Firebase getToken(messaging, { vapidKey }) 으로 받은 등록 토큰",
    )


class PushTokenResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    registered: bool = Field(..., description="이 기기가 지금 알림 대상인지")


class TestPushResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    sent: int = Field(..., description="보낸 기기 수. 무효 토큰은 지우고 세지 않는다")
