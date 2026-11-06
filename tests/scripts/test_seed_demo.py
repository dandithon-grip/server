import csv
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.supabase_admin import AdminUser
from app.clients.tmap import LatLng, PedestrianRoute
from app.core.config import get_settings
from app.domain.commute.models import Commute, Station
from app.domain.route.models import Route
from app.domain.route.service import distance_m
from app.domain.user.models import User
from scripts.seed_demo import DEMO_NICKNAME, SeedError, seed_demo

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo-password"


@dataclass
class FakeSupabaseAdmin:
    """Supabase Admin API 대역. 네트워크 없이 계정 목록을 메모리에 두고 생성 호출을 기록한다."""

    accounts: dict[str, AdminUser] = field(default_factory=dict)
    create_calls: list[str] = field(default_factory=list)

    async def find_user_by_email(self, email: str) -> AdminUser | None:
        return self.accounts.get(email)

    async def create_user(self, *, email: str, password: str, nickname: str) -> AdminUser:
        self.create_calls.append(email)
        account = AdminUser(id=f"fake-{len(self.accounts) + 1}", email=email)
        self.accounts[email] = account
        return account


async def find_user(session: AsyncSession, supabase_id: str) -> User | None:
    stmt = select(User).where(User.supabase_id == supabase_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def count_users(session: AsyncSession, supabase_id: str) -> int:
    stmt = select(func.count()).select_from(User).where(User.supabase_id == supabase_id)
    return (await session.execute(stmt)).scalar_one()


async def test_account_missing_creates_account_and_user_row(db_session: AsyncSession) -> None:
    admin = FakeSupabaseAdmin()

    result = await seed_demo(admin, db_session, email=DEMO_EMAIL, password=DEMO_PASSWORD)

    assert result.created_account is True
    assert admin.create_calls == [DEMO_EMAIL]
    user = await find_user(db_session, result.supabase_user_id)
    assert user is not None
    assert user.nickname == DEMO_NICKNAME
    assert user.is_demo is True  # 둘러보기 카드의 주인


async def test_account_exists_skips_creation_and_upserts_user_row(
    db_session: AsyncSession,
) -> None:
    existing = AdminUser(id="existing-account-id", email=DEMO_EMAIL)
    admin = FakeSupabaseAdmin(accounts={DEMO_EMAIL: existing})

    result = await seed_demo(admin, db_session, email=DEMO_EMAIL, password=DEMO_PASSWORD)

    assert result.created_account is False
    assert admin.create_calls == []
    assert result.supabase_user_id == "existing-account-id"
    assert await find_user(db_session, "existing-account-id") is not None


async def test_running_twice_is_idempotent(db_session: AsyncSession) -> None:
    admin = FakeSupabaseAdmin()

    first = await seed_demo(admin, db_session, email=DEMO_EMAIL, password=DEMO_PASSWORD)
    second = await seed_demo(admin, db_session, email=DEMO_EMAIL, password=DEMO_PASSWORD)

    assert first.supabase_user_id == second.supabase_user_id
    assert admin.create_calls == [DEMO_EMAIL]
    assert await count_users(db_session, first.supabase_user_id) == 1


# --- 동선·경로 ------------------------------------------------------------------

COMMUTE_HEADER = [
    "from_lat",
    "from_lng",
    "from_area",
    "to_lat",
    "to_lng",
    "to_area",
    "go_time",
    "leave_time",
    "transport_mode",
    "line",
    "station_names",
    "preference",
]
COMMUTE_ROW = [
    "35.8575",
    "128.6370",
    "수성구 범어동",
    "35.8690",
    "128.5960",
    "중구 동성로",
    "08:30",
    "18:10",
    "transit",
    "2",
    "수성구청|반월당",
    "full",
]


@dataclass
class FakeTmap:
    calls: int = 0

    async def pedestrian_route(
        self, start: LatLng, end: LatLng, pass_points: list[LatLng] | None = None
    ) -> PedestrianRoute:
        self.calls += 1
        path = [start, *(pass_points or []), end]
        length = sum(distance_m(a, b) for a, b in zip(path, path[1:], strict=False))
        return PedestrianRoute(minutes=length / 80, distance_m=int(length), polyline=path)


def write_commute_csv(path: Path, row: list[str] = COMMUTE_ROW) -> Path:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(COMMUTE_HEADER)
        writer.writerow(row)
    return path


async def add_line2(session: AsyncSession) -> None:
    coords = [
        ("반월당", 35.8655, 128.5934),
        ("경대병원", 35.8631, 128.6031),
        ("수성구청", 35.8589, 128.6358),
    ]
    session.add_all(
        Station(
            line="2",
            name=name,
            location=from_shape(Point(lng, lat), srid=4326),
            order_no=i,
            city=get_settings().default_city,
        )
        for i, (name, lat, lng) in enumerate(coords)
    )
    await session.flush()


async def test_seed_saves_demo_commute_and_routes(db_session: AsyncSession, tmp_path: Path) -> None:
    await add_line2(db_session)
    tmap = FakeTmap()

    result = await seed_demo(
        FakeSupabaseAdmin(),
        db_session,
        email=DEMO_EMAIL,
        password=DEMO_PASSWORD,
        commute_csv=write_commute_csv(tmp_path / "demo.csv"),
        tmap=tmap,
    )

    commute = (await db_session.execute(select(Commute))).scalar_one()
    assert commute.transport_mode == "transit"
    assert commute.line == "2"
    assert len(commute.station_ids) == 2
    routes = list((await db_session.execute(select(Route))).scalars())
    assert result.routes_count == len(routes) >= 3  # 구간별 기준선 최소 3개
    assert tmap.calls >= 3


async def test_seed_twice_keeps_one_demo_commute(db_session: AsyncSession, tmp_path: Path) -> None:
    await add_line2(db_session)
    csv_path = write_commute_csv(tmp_path / "demo.csv")

    for _ in range(2):
        await seed_demo(
            FakeSupabaseAdmin(),
            db_session,
            email=DEMO_EMAIL,
            password=DEMO_PASSWORD,
            commute_csv=csv_path,
            tmap=FakeTmap(),
        )

    commutes = list((await db_session.execute(select(Commute))).scalars())
    assert len(commutes) == 1


async def test_seed_without_stations_explains_prerequisite(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    with pytest.raises(SeedError, match="seed_stations"):
        await seed_demo(
            FakeSupabaseAdmin(),
            db_session,
            email=DEMO_EMAIL,
            password=DEMO_PASSWORD,
            commute_csv=write_commute_csv(tmp_path / "demo.csv"),
            tmap=FakeTmap(),
        )
