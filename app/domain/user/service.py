from fastapi import Depends
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.exceptions import AppException, ErrorCode
from app.domain.user.models import User

DEFAULT_NICKNAME = "샛길러"


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_user(self, supabase_id: str, nickname: str | None) -> User:
        """
        Supabase user id 로 사용자를 조회하거나, 최초 요청이면 생성한다.

        :param supabase_id: 검증이 끝난 JWT 의 sub 클레임
        :param nickname: JWT 의 user_metadata.nickname. 없으면 기본 닉네임으로 만든다
        :return: 조회 또는 생성된 User
        """
        # 1. 기존 사용자 조회 (대부분의 요청은 여기서 끝난다)
        stmt = select(User).where(User.supabase_id == supabase_id)
        user = (await self._session.execute(stmt)).scalar_one_or_none()
        if user is not None:
            return user

        # 2. 최초 로그인 - 생성. 같은 사용자의 첫 요청이 동시에 둘 오면 unique 충돌 대신
        #    한쪽만 들어가고 나머지는 무시된다 (INSERT ... ON CONFLICT DO NOTHING)
        insert_stmt = (
            insert(User)
            .values(
                supabase_id=supabase_id,
                nickname=nickname or DEFAULT_NICKNAME,
                city=get_settings().default_city,
            )
            .on_conflict_do_nothing(index_elements=["supabase_id"])
        )
        await self._session.execute(insert_stmt)

        # 3. 방금 들어간 (또는 동시 요청이 넣은) 행을 읽는다
        user = (await self._session.execute(stmt)).scalar_one()
        await self._session.commit()
        return user

    async def mark_demo(self, supabase_id: str) -> User:
        """
        계정을 둘러보기 카드의 주인으로 표시한다. 기존 데모 표시는 지운다 (데모는 하나).

        :raises AppException: USER_NOT_FOUND - 그 supabase_id 의 사용자가 없음
        """
        # 1. 대상 확인
        stmt = select(User).where(User.supabase_id == supabase_id)
        user = (await self._session.execute(stmt)).scalar_one_or_none()
        if user is None:
            raise AppException(ErrorCode.USER_NOT_FOUND)

        # 2. 데모는 하나만. 다른 행의 표시를 내리고 이 행을 켠다
        await self._session.execute(update(User).values(is_demo=False).where(User.is_demo))
        user.is_demo = True
        await self._session.commit()
        return user

    async def find_demo_user(self) -> User | None:
        """둘러보기 카드의 주인. 시드가 안 돌았으면 None."""
        stmt = select(User).where(User.is_demo).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none()


def get_user_service(session: AsyncSession = Depends(get_db)) -> UserService:
    return UserService(session)
