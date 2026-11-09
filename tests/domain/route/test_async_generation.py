"""샛길 생성 비동기 기대 동작 (#97).

`POST /commutes/me/routes?wait=false` 는 생성을 백그라운드로 시작하고 202 로 바로 답한다.
`GET /commutes/me/routes/status` 로 진행 상태를 본다. 기본(wait 생략)은 종전처럼 동기 200.
"""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.commute.models import Commute
from app.domain.route.models import PlaceType, Route
from tests.conftest import TokenFactory
from tests.domain.route.test_routes import bearer, place, use_fake_tmap, walk_commute


async def test_status_before_any_generation_is_idle(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    use_fake_tmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)

    response = await client.get("/commutes/me/routes/status", headers=headers)

    assert response.status_code == 200
    assert response.json()["state"] == "idle"
    assert response.json()["route_count"] == 0


async def test_async_generation_returns_202_then_finishes_in_background(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    db_session.add(place("공원", 35.8605, 128.6050, [PlaceType.PARK]))
    await db_session.flush()
    use_fake_tmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)

    started = await client.post("/commutes/me/routes?wait=false", headers=headers)

    assert started.status_code == 202
    assert started.json()["state"] == "pending"
    # 테스트 클라이언트는 백그라운드 작업까지 끝낸 뒤 돌아온다
    status = await client.get("/commutes/me/routes/status", headers=headers)
    assert status.json()["state"] == "ready"
    assert status.json()["route_count"] >= 1
    routes = list((await db_session.execute(select(Route))).scalars())
    assert routes
    listed = await client.get("/commutes/me/routes", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["shortest"]


async def test_sync_generation_is_still_the_default(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    use_fake_tmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)

    response = await client.post("/commutes/me/routes", headers=headers)

    assert response.status_code == 200
    assert "shortest" in response.json()
    status = await client.get("/commutes/me/routes/status", headers=headers)
    assert status.json()["state"] == "ready"


async def test_background_failure_is_reported_as_failed(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    from app.clients.tmap import get_tmap_client
    from app.core.exceptions import AppException, ErrorCode
    from app.main import app

    class BrokenTmap:
        async def pedestrian_route(self, *args: object, **kwargs: object) -> None:
            raise AppException(ErrorCode.EXTERNAL_API_FAILED)

    app.dependency_overrides[get_tmap_client] = lambda: BrokenTmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)

    started = await client.post("/commutes/me/routes?wait=false", headers=headers)

    assert started.status_code == 202
    status = await client.get("/commutes/me/routes/status", headers=headers)
    assert status.json()["state"] == "failed"


async def test_pending_older_than_five_minutes_is_reported_as_failed(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    # 서버가 생성 도중 재시작되면 pending 이 남는다. 오래된 pending 은 실패로 보여 다시 시도하게 한다
    use_fake_tmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)
    await db_session.execute(
        update(Commute).values(
            routes_state="pending", routes_updated_at=datetime.now(UTC) - timedelta(minutes=6)
        )
    )
    await db_session.flush()

    response = await client.get("/commutes/me/routes/status", headers=headers)

    assert response.json()["state"] == "failed"
