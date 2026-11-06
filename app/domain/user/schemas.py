from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    nickname: str
    city: str = Field(..., description="기본 도시 코드")
    notify_on: bool = Field(..., description="출퇴근 알림 수신 여부")
