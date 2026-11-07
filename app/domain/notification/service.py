"""F-13 퇴근 알림 (#63). FCM 웹 푸시 토큰을 관리하고 카드 알림을 보낸다."""

import logging
from uuid import UUID

from fastapi import Depends
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.fcm import FcmClient, get_fcm_client
from app.core.config import get_settings
from app.core.database import get_db
from app.core.exceptions import AppException, ErrorCode
from app.domain.notification.models import PushToken

logger = logging.getLogger(__name__)

CARD_TITLE = "오늘의 샛길"
TEST_TITLE = "샛길 알림 테스트"
TEST_BODY = "퇴근 10분 전에 이렇게 오늘의 샛길을 알려드려요."


class NotificationService:
    def __init__(self, session: AsyncSession, fcm: FcmClient) -> None:
        self._session = session
        self._fcm = fcm

    @property
    def enabled(self) -> bool:
        """FCM 키가 설정돼 알림을 보낼 수 있는지."""
        return self._fcm.configured

    async def register(self, user_id: UUID, token: str) -> None:
        """
        기기 토큰을 등록한다. 이미 있으면 이 사용자로 옮긴다 (한 브라우저에서 계정을 바꾼 경우).

        :param user_id: 로그인 사용자
        :param token: Firebase 등록 토큰
        """
        # 1. upsert. token unique 충돌이면 주인만 바꾼다
        stmt = insert(PushToken).values(user_id=user_id, token=token)
        stmt = stmt.on_conflict_do_update(index_elements=["token"], set_={"user_id": user_id})
        await self._session.execute(stmt)
        await self._session.commit()

    async def unregister(self, user_id: UUID, token: str) -> None:
        """내 기기 토큰을 지운다 (알림 끄기·로그아웃). 없으면 아무것도 하지 않는다."""
        stmt = delete(PushToken).where(PushToken.user_id == user_id, PushToken.token == token)
        await self._session.execute(stmt)
        await self._session.commit()

    async def send_test(self, user_id: UUID) -> int:
        """
        내 기기 전부에 테스트 알림을 지금 보낸다.

        :return: 보낸 기기 수
        :raises AppException: NOTIFICATION_NOT_CONFIGURED - FCM 키가 없음
        """
        if not self._fcm.configured:
            raise AppException(ErrorCode.NOTIFICATION_NOT_CONFIGURED)
        return await self.notify(user_id, title=TEST_TITLE, body=TEST_BODY)

    async def notify(self, user_id: UUID, *, title: str, body: str) -> int:
        """
        사용자의 기기 전부에 알림. 무효 토큰(404)은 지운다. 미설정이면 0 을 돌려준다.

        :return: 보낸 기기 수
        """
        # 1. 미설정이면 조용히 건너뛴다 (카드는 그대로 만들어진다)
        if not self._fcm.configured:
            return 0

        # 2. 기기마다 발송. 한 기기의 실패가 나머지를 막지 않는다
        stmt = select(PushToken).where(PushToken.user_id == user_id)
        tokens = list((await self._session.execute(stmt)).scalars())
        link = get_settings().frontend_url or None
        sent = 0
        for push_token in tokens:
            try:
                delivered = await self._fcm.send(
                    push_token.token, title=title, body=body, link=link
                )
            except AppException:
                logger.warning("알림 발송 실패, 다음 기기로")
                continue
            if delivered:
                sent += 1
            else:
                await self._session.delete(push_token)

        # 3. 무효 토큰 삭제 반영
        await self._session.commit()
        return sent


def get_notification_service(
    session: AsyncSession = Depends(get_db), fcm: FcmClient = Depends(get_fcm_client)
) -> NotificationService:
    return NotificationService(session, fcm)
