"""데모 계정·동선 시드 스크립트.

    uv run python -m scripts.seed_demo

1. Supabase Admin API 로 DEMO_EMAIL 계정을 찾고, 없으면 만든다
   (user_metadata.nickname = "데모", 이메일 인증 완료 상태)
2. users 테이블에 그 계정의 행을 upsert 하고 둘러보기 카드의 주인(is_demo)으로 표시한다
3. data/demo_commute.csv 의 동선을 저장한다 (정류장은 stations 에서 이름으로 찾는다)
4. 걷는 구간마다 TMAP 으로 경로를 미리 만든다 (약 20회). 재실행하면 지우고 다시 만든다

실행 순서: seed_stations → seed_places → seed_demo. 정류장이 없으면 멈춘다.
SUPABASE_SERVICE_ROLE_KEY, DEMO_EMAIL, DEMO_PASSWORD 는 이 스크립트의 SeedSettings 만 읽는다.
앱의 Settings(app/core/config.py)에는 없다.
"""

import asyncio
import csv
import sys
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.supabase_admin import AdminUser, SupabaseAdminClient
from app.clients.tmap import LatLng, PedestrianRoute, TmapClient
from app.core.config import get_settings
from app.core.database import session_factory
from app.domain.commute.models import Preference, TransportMode
from app.domain.commute.schemas import SaveCommuteRequest
from app.domain.commute.service import CommuteService
from app.domain.route.service import RouteService
from app.domain.user.service import UserService

DEMO_NICKNAME = "데모"
DEFAULT_COMMUTE_CSV = Path("data/demo_commute.csv")


class SeedError(Exception):
    """CSV 값이 잘못됐거나 선행 시드(정류장)가 없을 때."""


class DemoCommuteRow(BaseModel):
    """data/demo_commute.csv 한 줄."""

    model_config = ConfigDict(frozen=True)

    from_lat: float = Field(..., ge=-90, le=90)
    from_lng: float = Field(..., ge=-180, le=180)
    from_area: str = Field(..., min_length=1, max_length=50)
    to_lat: float = Field(..., ge=-90, le=90)
    to_lng: float = Field(..., ge=-180, le=180)
    to_area: str = Field(..., min_length=1, max_length=50)
    go_time: time
    leave_time: time
    transport_mode: TransportMode
    line: str | None = None
    station_names: list[str] = Field(default_factory=list)
    preference: Preference = Preference.FULL

    @field_validator("station_names", mode="before")
    @classmethod
    def split_names(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split("|") if part.strip()]
        return value

    @field_validator("line", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class Tmap(Protocol):
    """경로 생성에 필요한 TMAP 동작. 테스트는 직선 경로를 돌려주는 대역을 넣는다."""

    async def pedestrian_route(
        self, start: LatLng, end: LatLng, pass_points: list[LatLng] | None = None
    ) -> PedestrianRoute: ...


def read_demo_commute(csv_path: Path) -> DemoCommuteRow:
    """CSV 첫 행을 읽어 검증한다."""
    with csv_path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        raise SeedError(f"{csv_path} 에 동선 행이 없다")
    try:
        return DemoCommuteRow.model_validate(rows[0])
    except ValidationError as error:
        fields = ", ".join(str(item["loc"][0]) for item in error.errors())
        raise SeedError(f"{csv_path} 2행 값이 비었거나 잘못됐다: {fields}") from error


class SeedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    supabase_service_role_key: str = Field(..., min_length=1)
    demo_email: str = Field(..., min_length=1)
    demo_password: str = Field(..., min_length=6, description="Supabase 기본 최소 길이 6")


class SupabaseAdmin(Protocol):
    """seed_demo 가 필요로 하는 Admin API 동작. 테스트는 이 형태의 대역을 넣는다."""

    async def find_user_by_email(self, email: str) -> AdminUser | None: ...

    async def create_user(self, *, email: str, password: str, nickname: str) -> AdminUser: ...


@dataclass(frozen=True)
class SeedResult:
    supabase_user_id: str
    created_account: bool
    routes_count: int = 0


async def seed_demo(
    admin: SupabaseAdmin,
    session: AsyncSession,
    *,
    email: str,
    password: str,
    nickname: str = DEMO_NICKNAME,
    commute_csv: Path | None = None,
    tmap: Tmap | None = None,
) -> SeedResult:
    """
    데모 계정·users 행·동선·경로를 준비한다. 여러 번 실행해도 결과가 같다.

    :param commute_csv: 데모 동선 CSV. None 이면 동선·경로는 건너뛴다
    :param tmap: 경로 생성용 TMAP 클라이언트. commute_csv 가 있으면 필수
    :return: 계정의 Supabase user id, 계정을 새로 만들었는지, 만든 경로 수
    :raises AppException: EXTERNAL_API_FAILED - Admin API·TMAP 호출 실패
    :raises SeedError: CSV 값 오류, 정류장 시드 없음
    """
    # 1. 계정 조회
    account = await admin.find_user_by_email(email)
    created = account is None

    # 2. 없으면 생성
    if account is None:
        account = await admin.create_user(email=email, password=password, nickname=nickname)

    # 3. users 행 upsert (이미 있으면 그대로 둔다) + 둘러보기 카드의 주인으로 표시
    user_service = UserService(session)
    user = await user_service.get_or_create_user(supabase_id=account.id, nickname=nickname)
    await user_service.mark_demo(account.id)
    if commute_csv is None:
        return SeedResult(supabase_user_id=account.id, created_account=created)
    if tmap is None:
        raise SeedError("동선 CSV 가 있으면 tmap 이 필요하다")

    # 4. 동선 저장 (정류장은 이름으로 찾는다)
    row = read_demo_commute(commute_csv)
    commute_service = CommuteService(session)
    station_ids = await _station_ids(commute_service, row, user.city)
    commute = await commute_service.save_commute(
        user.id,
        SaveCommuteRequest(
            from_lat=row.from_lat,
            from_lng=row.from_lng,
            to_lat=row.to_lat,
            to_lng=row.to_lng,
            from_area=row.from_area,
            to_area=row.to_area,
            go_time=row.go_time,
            leave_time=row.leave_time,
            transport_mode=row.transport_mode,
            line=row.line,
            station_ids=station_ids,
            preference=row.preference,
        ),
        city=user.city,
    )

    # 5. 경로 사전 계산 (기존 경로는 지우고 다시 만든다)
    routes = await RouteService(session, tmap).generate(commute, user.city)  # type: ignore[arg-type]
    return SeedResult(
        supabase_user_id=account.id, created_account=created, routes_count=len(routes)
    )


async def _station_ids(service: CommuteService, row: DemoCommuteRow, city: str) -> list[UUID]:
    """정류장 이름 → id. 하나라도 없으면 먼저 seed_stations 를 돌리라고 멈춘다."""
    if row.transport_mode != TransportMode.TRANSIT:
        return []
    if row.line is None:
        raise SeedError("transit 동선은 line 이 필요하다")
    stations = await service.list_stations(line=row.line, city=city)
    by_name = {station.name: station.id for station in stations}
    missing = [name for name in row.station_names if name not in by_name]
    if missing:
        raise SeedError(
            f"정류장 {', '.join(missing)} 이(가) stations 에 없다. "
            "먼저 `uv run python -m scripts.seed_stations` 를 실행한다"
        )
    return [by_name[name] for name in row.station_names]


async def main(commute_csv: Path) -> None:
    seed_settings = SeedSettings()
    settings = get_settings()
    admin = SupabaseAdminClient(settings.supabase_url, seed_settings.supabase_service_role_key)
    tmap = TmapClient(settings.tmap_app_key)

    async with session_factory() as session:
        result = await seed_demo(
            admin,
            session,
            email=seed_settings.demo_email,
            password=seed_settings.demo_password,
            commute_csv=commute_csv,
            tmap=tmap,
        )

    state = "생성" if result.created_account else "이미 있음 (건너뜀)"
    print(f"데모 계정 {seed_settings.demo_email}: {state} / supabase_id={result.supabase_user_id}")
    print(f"데모 동선 저장, 경로 {result.routes_count}개 사전 계산 ({commute_csv})")


if __name__ == "__main__":
    try:
        asyncio.run(main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_COMMUTE_CSV))
    except SeedError as error:
        sys.exit(f"시드 중단: {error}")
