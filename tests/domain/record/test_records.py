"""F-11 기록 남기기 기대 동작 (비전 호출 전 단계).

- POST /records 는 사용자가 고친 season·stage 와 비전 제안 ai_season·ai_stage 를 둘 다 저장한다
- 둘러보기(토큰 없음)는 저장·조회 모두 401
- GET /records 는 본인 기록만 돌려주고 place_id 로 거를 수 있다
"""

import datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ErrorCode
from app.core.geo import make_point
from app.domain.record.models import Record, Season, Stage
from app.domain.route.models import Place, PlaceType
from tests.conftest import TokenFactory

LOCAL_TZ = ZoneInfo("Asia/Seoul")
OTHER_SUB = "99999999-9999-9999-9999-999999999999"
IMAGE_PATH = "11111111-1111-1111-1111-111111111111/2026-11-04/photo.jpg"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def add_place(session: AsyncSession, name: str = "신천 동신교") -> Place:
    place = Place(
        name=name,
        location=make_point(35.8681, 128.6005),
        types=[PlaceType.RIVER.value],
        description="물소리가 가까운 하천길",
        city=get_settings().default_city,
    )
    session.add(place)
    await session.flush()
    return place


def payload(place_id: UUID, **overrides: object) -> dict[str, object]:
    """비전이 foliage/peak 를 제안하고 사용자가 그대로 받아들인 요청."""
    body: dict[str, object] = {
        "place_id": str(place_id),
        "season": "foliage",
        "stage": "peak",
        "ai_season": "foliage",
        "ai_stage": "peak",
        "title": "은행잎 깔린 길",
        "image_path": IMAGE_PATH,
    }
    body.update(overrides)
    return body


async def stored_records(session: AsyncSession) -> list[Record]:
    stmt = select(Record).order_by(Record.created_at)
    return list((await session.execute(stmt)).scalars())


# --- 인증 -------------------------------------------------------------------


async def test_guest_post_returns_401(client: AsyncClient, db_session: AsyncSession) -> None:
    place = await add_place(db_session)

    response = await client.post("/records", json=payload(place.id))

    assert response.status_code == 401
    assert response.json() == {"message": ErrorCode.INVALID_TOKEN.message}


async def test_guest_get_returns_401(client: AsyncClient) -> None:
    response = await client.get("/records")

    assert response.status_code == 401


# --- 저장 -------------------------------------------------------------------


async def test_saves_user_choice_and_ai_suggestion(
    client: AsyncClient, db_session: AsyncSession, make_token: TokenFactory
) -> None:
    place = await add_place(db_session)

    response = await client.post("/records", json=payload(place.id), headers=bearer(make_token()))

    assert response.status_code == 201
    body = response.json()
    assert body["season"] == Season.FOLIAGE
    assert body["stage"] == Stage.PEAK
    assert body["ai_season"] == Season.FOLIAGE
    assert body["ai_stage"] == Stage.PEAK
    assert body["place_id"] == str(place.id)
    assert body["image_path"] == IMAGE_PATH
    assert body["title"] == "은행잎 깔린 길"

    records = await stored_records(db_session)
    assert len(records) == 1
    assert records[0].season == Season.FOLIAGE
    assert records[0].ai_season == Season.FOLIAGE


async def test_user_override_is_saved_apart_from_ai_suggestion(
    client: AsyncClient, db_session: AsyncSession, make_token: TokenFactory
) -> None:
    """사용자가 비전 제안을 고치면 season ≠ ai_season 으로 둘 다 남는다."""
    place = await add_place(db_session)
    body = payload(place.id, season="ginkgo", stage="end")

    response = await client.post("/records", json=body, headers=bearer(make_token()))

    assert response.status_code == 201
    record = (await stored_records(db_session))[0]
    assert record.season == Season.GINKGO
    assert record.ai_season == Season.FOLIAGE
    assert record.stage == Stage.END
    assert record.ai_stage == Stage.PEAK


async def test_vision_fallback_none_is_saved(
    client: AsyncClient, db_session: AsyncSession, make_token: TokenFactory
) -> None:
    """비전이 스키마 밖 답을 줘서 none 으로 폴백한 경우도 그대로 저장된다."""
    place = await add_place(db_session)
    body = payload(place.id, season="cherry", stage="mid", ai_season="none", ai_stage="none")

    response = await client.post("/records", json=body, headers=bearer(make_token()))

    assert response.status_code == 201
    record = (await stored_records(db_session))[0]
    assert record.ai_season == Season.NONE
    assert record.ai_stage == Stage.NONE


async def test_without_vision_ai_fields_are_null(
    client: AsyncClient, db_session: AsyncSession, make_token: TokenFactory
) -> None:
    """비전을 부르지 않고 직접 고른 기록은 ai_* 가 비어 있다 (none 과 구분한다)."""
    place = await add_place(db_session)
    body = payload(place.id)
    del body["ai_season"]
    del body["ai_stage"]
    del body["title"]

    response = await client.post("/records", json=body, headers=bearer(make_token()))

    assert response.status_code == 201
    assert response.json()["ai_season"] is None
    record = (await stored_records(db_session))[0]
    assert record.ai_season is None
    assert record.ai_stage is None
    assert record.title is None


async def test_date_defaults_to_today(
    client: AsyncClient, db_session: AsyncSession, make_token: TokenFactory
) -> None:
    place = await add_place(db_session)

    response = await client.post("/records", json=payload(place.id), headers=bearer(make_token()))

    assert response.json()["date"] == datetime.datetime.now(LOCAL_TZ).date().isoformat()


async def test_given_date_is_saved(
    client: AsyncClient, db_session: AsyncSession, make_token: TokenFactory
) -> None:
    place = await add_place(db_session)

    response = await client.post(
        "/records", json=payload(place.id, date="2026-11-01"), headers=bearer(make_token())
    )

    assert response.json()["date"] == "2026-11-01"


async def test_unknown_place_returns_404(client: AsyncClient, make_token: TokenFactory) -> None:
    response = await client.post("/records", json=payload(uuid4()), headers=bearer(make_token()))

    assert response.status_code == 404
    assert response.json() == {"message": ErrorCode.PLACE_NOT_FOUND.message}


async def test_unknown_season_returns_422(
    client: AsyncClient, db_session: AsyncSession, make_token: TokenFactory
) -> None:
    place = await add_place(db_session)

    response = await client.post(
        "/records", json=payload(place.id, season="rain"), headers=bearer(make_token())
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "image_path", ["/absolute/photo.jpg", "../other-user/photo.jpg", "uid/../other-user/photo.jpg"]
)
async def test_image_path_outside_bucket_returns_422(
    client: AsyncClient, db_session: AsyncSession, make_token: TokenFactory, image_path: str
) -> None:
    """버킷 밖을 가리키는 경로는 어차피 내려받을 수 없다. 저장 시점에 거른다."""
    place = await add_place(db_session)

    response = await client.post(
        "/records", json=payload(place.id, image_path=image_path), headers=bearer(make_token())
    )

    assert response.status_code == 422
    assert await stored_records(db_session) == []


async def test_place_with_records_cannot_be_deleted(
    client: AsyncClient, db_session: AsyncSession, make_token: TokenFactory
) -> None:
    """큐레이션 지점을 지운다고 사용자 기록이 따라 사라지면 안 된다.

    기록이 있는 지점은 삭제가 거부된다 (RESTRICT).
    """
    place = await add_place(db_session)
    await client.post("/records", json=payload(place.id), headers=bearer(make_token()))

    await db_session.delete(place)
    with pytest.raises(IntegrityError):
        await db_session.flush()


# --- 조회 -------------------------------------------------------------------


async def test_lists_only_own_records(
    client: AsyncClient, db_session: AsyncSession, make_token: TokenFactory
) -> None:
    place = await add_place(db_session)
    await client.post("/records", json=payload(place.id), headers=bearer(make_token()))

    response = await client.get("/records", headers=bearer(make_token(sub=OTHER_SUB)))

    assert response.status_code == 200
    assert response.json() == []


async def test_place_id_filters_records(
    client: AsyncClient, db_session: AsyncSession, make_token: TokenFactory
) -> None:
    river = await add_place(db_session, name="신천 동신교")
    park = await add_place(db_session, name="경상감영공원")
    token = make_token()
    await client.post("/records", json=payload(river.id), headers=bearer(token))
    await client.post("/records", json=payload(park.id), headers=bearer(token))

    response = await client.get(
        "/records", params={"place_id": str(park.id)}, headers=bearer(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["place_id"] for item in body] == [str(park.id)]


async def test_lists_newest_first(
    client: AsyncClient, db_session: AsyncSession, make_token: TokenFactory
) -> None:
    place = await add_place(db_session)
    token = make_token()
    await client.post("/records", json=payload(place.id, date="2026-11-01"), headers=bearer(token))
    await client.post("/records", json=payload(place.id, date="2026-11-03"), headers=bearer(token))

    response = await client.get("/records", headers=bearer(token))

    assert [item["date"] for item in response.json()] == ["2026-11-03", "2026-11-01"]
