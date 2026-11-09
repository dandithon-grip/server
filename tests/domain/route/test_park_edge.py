"""공원 안 자연 지점은 공원 가장자리로 옮겨 경유한다 (왕복 필터에 공원 후보가 죽는 문제).

큐레이션 공원 지점은 공원 한가운데라 TMAP 이 들어갔다 같은 길로 나온다 → #49 왕복 필터에 탈락.
폴리곤 경계에서 기준선에 가장 가까운 점을 경유 좌표로 쓰면 공원을 지나가는 경로가 된다.
"""

from uuid import uuid4

from geoalchemy2.shape import from_shape
from httpx import AsyncClient
from shapely.geometry import Polygon
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tmap import LatLng
from app.core.config import get_settings
from app.domain.commute.models import Preference
from app.domain.route.models import GreenArea, GreenKind, PlaceType
from app.domain.route.service import RouteService, distance_m, shortlist
from tests.conftest import TokenFactory
from tests.domain.route.test_routes import A, B, bearer, place, use_fake_tmap, walk_commute

CITY = get_settings().default_city
# 기준선 A→B 는 위도 35.8600 의 동서 직선. 공원은 선에서 북쪽으로 150~350m 떨어진 사각형
PARK_SOUTH = 35.8600 + 150 / 110_540
PARK_NORTH = 35.8600 + 350 / 110_540
PARK = Polygon(
    [(128.6040, PARK_SOUTH), (128.6070, PARK_SOUTH), (128.6070, PARK_NORTH), (128.6040, PARK_NORTH)]
)
PARK_CENTER = (35.8600 + 250 / 110_540, 128.6055)
CENTER = LatLng(lat=PARK_CENTER[0], lng=PARK_CENTER[1])


def park_area() -> GreenArea:
    return GreenArea(
        id=uuid4(),
        kind=GreenKind.PARK.value,
        name="공원",
        geom=from_shape(PARK, srid=4326),
        city=CITY,
    )


def test_shortlist_uses_entry_points_for_pass_points_and_detour() -> None:
    inside = place("공원", *PARK_CENTER, [PlaceType.PARK])
    edge = LatLng(lat=PARK_SOUTH, lng=128.6055)

    waypoints = shortlist(A, B, [inside], Preference.FULL, entry_points={inside.id: edge})

    assert len(waypoints) == 1
    assert waypoints[0].pass_points == [edge]
    assert waypoints[0].places == (inside,)
    # 우회량도 옮긴 점 기준이라 중심점보다 작다
    assert waypoints[0].detour_m < distance_m(A, CENTER) + distance_m(CENTER, B) - distance_m(A, B)


async def test_place_inside_park_moves_to_the_edge_nearest_the_baseline(
    db_session: AsyncSession,
) -> None:
    inside = place("공원", *PARK_CENTER, [PlaceType.PARK])
    outside = place("다리", 35.8605, 128.6030, [PlaceType.BRIDGE])
    db_session.add_all([park_area(), inside, outside])
    await db_session.flush()
    service = RouteService(db_session, use_fake_tmap())

    entries = await service.entry_points([inside, outside], [A, B], CITY)

    assert set(entries) == {inside.id}
    moved = entries[inside.id]
    # 남쪽 경계(기준선 쪽) 위, 중심과 같은 경도
    assert abs(moved.lat - PARK_SOUTH) < 1e-6
    assert abs(moved.lng - PARK_CENTER[1]) < 1e-4


async def test_generate_routes_via_park_edge_not_park_center(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    db_session.add_all([park_area(), place("공원", *PARK_CENTER, [PlaceType.PARK])])
    await db_session.flush()
    fake = use_fake_tmap()
    headers = bearer(make_token())
    await client.put("/commutes/me", json=walk_commute(), headers=headers)

    response = await client.post("/commutes/me/routes", headers=headers)

    assert response.status_code == 200
    via = [call[1] for call in fake.calls if len(call) == 3 and call[0] == A and call[-1] == B]
    assert via, "경유지 TMAP 호출이 있어야 한다"
    assert all(abs(point.lat - PARK_SOUTH) < 1e-6 for point in via)  # 중심이 아니라 남쪽 경계
    assert any(r["leg_type"] == "walk_all" for r in response.json()["candidates"])
