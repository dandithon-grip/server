"""FCM 푸시 토큰 등록·테스트 발송 기대 동작 (#63)."""

from dataclasses import dataclass, field

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.fcm import get_fcm_client
from app.core.exceptions import ErrorCode
from app.domain.notification.models import PushToken
from app.main import app
from tests.conftest import TokenFactory

USER_B = "bbbbbbbb-0000-0000-0000-000000000002"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class FakeFcm:
    """보낸 알림을 기록한다. dead 에 든 토큰은 FCM 이 404(UNREGISTERED)로 답한 것처럼."""

    configured: bool = True
    dead: set[str] = field(default_factory=set)
    sent: list[tuple[str, str, str]] = field(default_factory=list)

    async def send(self, token: str, *, title: str, body: str, link: str | None) -> bool:
        if token in self.dead:
            return False
        self.sent.append((token, title, body))
        return True


def use_fcm(fake: FakeFcm) -> FakeFcm:
    app.dependency_overrides[get_fcm_client] = lambda: fake
    return fake


async def tokens(session: AsyncSession) -> list[PushToken]:
    return list((await session.execute(select(PushToken))).scalars())


async def test_register_push_token(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    use_fcm(FakeFcm())

    response = await client.put(
        "/users/me/push-tokens", json={"token": "device-a"}, headers=bearer(make_token())
    )

    assert response.status_code == 200
    assert response.json() == {"registered": True}
    assert [t.token for t in await tokens(db_session)] == ["device-a"]


async def test_same_device_registered_by_another_user_moves_to_that_user(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 한 브라우저에서 계정을 바꾸면 알림은 마지막 로그인 사용자에게만 간다
    use_fcm(FakeFcm())
    body = {"token": "shared-device"}
    await client.put("/users/me/push-tokens", json=body, headers=bearer(make_token()))

    await client.put("/users/me/push-tokens", json=body, headers=bearer(make_token(sub=USER_B)))

    rows = await tokens(db_session)
    assert len(rows) == 1


async def test_unregister_push_token(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    use_fcm(FakeFcm())
    headers = bearer(make_token())
    await client.put("/users/me/push-tokens", json={"token": "device-a"}, headers=headers)

    response = await client.request(
        "DELETE", "/users/me/push-tokens", json={"token": "device-a"}, headers=headers
    )

    assert response.status_code == 200
    assert await tokens(db_session) == []


async def test_register_without_login_returns_401(client: AsyncClient) -> None:
    response = await client.put("/users/me/push-tokens", json={"token": "x"})

    assert response.status_code == 401


async def test_test_push_sends_to_my_devices_and_drops_dead_ones(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    fake = use_fcm(FakeFcm(dead={"old-device"}))
    headers = bearer(make_token())
    for device in ("phone", "old-device"):
        await client.put("/users/me/push-tokens", json={"token": device}, headers=headers)

    response = await client.post("/users/me/push-tokens/test", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"sent": 1}
    assert [token for token, _, _ in fake.sent] == ["phone"]
    assert [t.token for t in await tokens(db_session)] == ["phone"]


async def test_test_push_without_fcm_settings_returns_503(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    use_fcm(FakeFcm(configured=False))

    response = await client.post("/users/me/push-tokens/test", headers=bearer(make_token()))

    assert response.status_code == 503
    assert response.json() == {"message": ErrorCode.NOTIFICATION_NOT_CONFIGURED.message}
