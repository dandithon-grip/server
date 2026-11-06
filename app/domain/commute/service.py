from dataclasses import dataclass, field
from uuid import UUID

from fastapi import Depends
from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.odsay import OdsayClient, TransitPath, get_odsay_client
from app.clients.tmap import LatLng
from app.core.database import get_db
from app.core.exceptions import AppException, ErrorCode
from app.core.geo import make_point
from app.domain.commute.models import Commute, Station, TransportMode
from app.domain.commute.schemas import SaveCommuteRequest, TransitLegInput, TransitStopInput

MAX_SKIP_STOPS = 3  # 먼저 내리기·늦게 타기로 건너뛰는 정거장 상한 (#51)


@dataclass(frozen=True)
class SkipStations:
    """늦게 탈 정류장(탑승역에서 가까운 순)과 먼저 내릴 정류장(하차역에서 가까운 순)."""

    board_late: list[UUID] = field(default_factory=list)
    alight_early: list[UUID] = field(default_factory=list)


def skip_stations(first_ride: list[UUID], last_ride: list[UUID]) -> SkipStations:
    """
    첫 탑승 구간의 정류장 목록과 마지막 탑승 구간의 정류장 목록에서 1~3 정거장을 고른다.

    구간의 탑승역·하차역 자체는 건너뛸 수 없다 (그러면 타지 않는 것이다).
    """
    board_late = first_ride[1:-1][:MAX_SKIP_STOPS]
    alight_early = list(reversed(last_ride[1:-1]))[:MAX_SKIP_STOPS]
    return SkipStations(board_late=board_late, alight_early=alight_early)


class CommuteService:
    def __init__(self, session: AsyncSession, odsay: OdsayClient | None = None) -> None:
        self._session = session
        self._odsay = odsay

    async def search_transit(self, start: LatLng, end: LatLng) -> list[TransitPath]:
        """
        출발·도착 좌표로 대중교통 경로 후보를 받는다 (ODsay 1회).

        :raises AppException: EXTERNAL_API_FAILED - ODsay 실패
        """
        if self._odsay is None:
            raise AppException(ErrorCode.EXTERNAL_API_FAILED)
        return await self._odsay.search_transit(start, end)

    async def save_commute(
        self, user_id: UUID, request: SaveCommuteRequest, *, city: str
    ) -> Commute:
        """
        출퇴근 동선을 저장한다. 이미 있으면 덮어쓴다 (사용자당 1행).

        :param user_id: 저장할 사용자 (users.id)
        :param request: 검증이 끝난 요청. 좌표·동 이름·시각·교통수단·정류장·취향
        :param city: 정류장을 저장할 도시 코드
        :return: 저장된 Commute
        :raises AppException: STATION_REQUIRED - transit 인데 정류장이 없음
        :raises AppException: STATION_NOT_FOUND - 모르는 정류장 id 가 있음
        """
        # 1. 교통수단별 정류장 규칙. transit 만 정류장을 쓰고 나머지는 비운다
        is_transit = request.transport_mode == TransportMode.TRANSIT
        if is_transit and not request.station_ids and not request.transit_legs:
            raise AppException(ErrorCode.STATION_REQUIRED)
        station_ids: list[UUID] = []
        skips = SkipStations()
        line = request.line if is_transit else None
        if is_transit and request.transit_legs:
            # 고른 경로의 탑승 구간들을 stations 에 넣고 구간마다 탑승·하차 역 id 를 순서대로.
            # 1~3 정거장 먼저 내리기·늦게 타기 정류장도 이 사용자의 구간에서 지금 정한다
            station_ids, line, skips = await self._store_transit_legs(request.transit_legs, city)
        elif is_transit:
            station_ids = list(request.station_ids)

        # 2. 정류장이 실제로 있는지 확인
        if station_ids:
            count_stmt = (
                select(func.count()).select_from(Station).where(Station.id.in_(station_ids))
            )
            found = (await self._session.execute(count_stmt)).scalar_one()
            if found != len(set(station_ids)):
                raise AppException(ErrorCode.STATION_NOT_FOUND)

        # 3. 시드 정류장 동선은 노선 순번으로 1~3 정거장을 찾는다
        if station_ids and not request.transit_legs:
            skips = await self._seeded_skip_stations(station_ids[0], station_ids[-1])

        # 4. upsert. user_id unique 충돌이면 모든 값을 갱신한다
        values = {
            "user_id": user_id,
            "from_location": from_shape(Point(request.from_lng, request.from_lat), srid=4326),
            "to_location": from_shape(Point(request.to_lng, request.to_lat), srid=4326),
            "from_area": request.from_area,
            "to_area": request.to_area,
            "go_time": request.go_time,
            "leave_time": request.leave_time,
            "transport_mode": request.transport_mode.value,
            "line": line,
            "station_ids": station_ids,
            "alight_early_station_ids": skips.alight_early,
            "board_late_station_ids": skips.board_late,
            "preference": request.preference.value,
        }
        upsert_stmt = insert(Commute).values(**values)
        upsert_stmt = upsert_stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={key: value for key, value in values.items() if key != "user_id"},
        )
        await self._session.execute(upsert_stmt)

        # 5. 저장된 행을 읽고 commit
        commute = await self._find_by_user(user_id)
        await self._session.commit()
        assert commute is not None
        return commute

    async def get_commute(self, user_id: UUID) -> Commute:
        """
        사용자의 출퇴근 동선을 조회한다.

        :raises AppException: COMMUTE_NOT_FOUND - 아직 저장하지 않음
        """
        # 1. 조회
        commute = await self._find_by_user(user_id)
        if commute is None:
            raise AppException(ErrorCode.COMMUTE_NOT_FOUND)
        return commute

    async def _store_transit_legs(
        self, legs: list[TransitLegInput], city: str
    ) -> tuple[list[UUID], str, SkipStations]:
        """탑승 구간의 정류장을 ODsay stationID 기준으로 upsert 한다.

        :return: [탑승, 하차, 탑승, 하차, ...] 순서의 id 목록, 마지막 노선,
                 첫 구간에서 늦게 탈 정류장 · 마지막 구간에서 먼저 내릴 정류장 (각 1~3개)
        """
        station_ids: list[UUID] = []
        stop_ids: list[list[UUID]] = []  # 구간별 정류장 id, 순서대로
        for leg in legs:
            # 1. upsert. 순환 버스처럼 같은 정류장이 두 번 나오면 첫 번째만 넣는다
            #    (한 INSERT 문에 같은 키가 둘이면 Postgres 가 에러를 낸다)
            unique_stops: dict[str, tuple[int, TransitStopInput]] = {}
            for order, stop in enumerate(leg.stops):
                unique_stops.setdefault(stop.external_id, (order, stop))
            values = [
                {
                    "city": city,
                    "line": leg.line,
                    "name": stop.name,
                    "external_id": stop.external_id,
                    "order_no": order,
                    "location": make_point(stop.lat, stop.lng),
                }
                for order, stop in unique_stops.values()
            ]
            upsert = insert(Station).values(values)
            upsert = upsert.on_conflict_do_update(
                constraint="uq_stations_city_line_external",
                set_={
                    "name": upsert.excluded.name,
                    "order_no": upsert.excluded.order_no,
                    "location": upsert.excluded.location,
                },
            )
            await self._session.execute(upsert)

            # 2. 이 구간 정류장의 id 를 stationID 로 찾는다
            stmt = select(Station.external_id, Station.id).where(
                Station.city == city,
                Station.line == leg.line,
                Station.external_id.in_(list(unique_stops)),
            )
            id_of = {ext: sid for ext, sid in (await self._session.execute(stmt)).all()}
            ids = [id_of[stop.external_id] for stop in leg.stops]
            station_ids += [ids[0], ids[-1]]
            stop_ids.append(ids)

        # 3. 늦게 타기는 첫 구간, 먼저 내리기는 마지막 구간
        return station_ids, legs[-1].line, skip_stations(stop_ids[0], stop_ids[-1])

    async def _seeded_skip_stations(self, board_id: UUID, alight_id: UUID) -> SkipStations:
        """시드 정류장(stationID 없음) 동선. 같은 노선 순번으로 탑승·하차역 사이를 채운다."""
        # 1. 탑승·하차역
        stmt = select(Station).where(Station.id.in_([board_id, alight_id]))
        by_id = {s.id: s for s in (await self._session.execute(stmt)).scalars()}
        board, alight = by_id[board_id], by_id[alight_id]
        if board.line != alight.line or board.external_id or alight.external_id:
            return SkipStations()

        # 2. 같은 노선에서 두 역 사이 정류장을 진행 방향 순서로
        low, high = sorted((board.order_no, alight.order_no))
        between_stmt = select(Station).where(
            Station.city == board.city,
            Station.line == board.line,
            Station.external_id.is_(None),
            Station.order_no > low,
            Station.order_no < high,
        )
        between = sorted(
            (await self._session.execute(between_stmt)).scalars(), key=lambda s: s.order_no
        )
        if board.order_no > alight.order_no:
            between.reverse()
        ride = [board.id, *(s.id for s in between), alight.id]
        return skip_stations(ride, ride)

    async def list_stations(self, *, line: str, city: str) -> list[Station]:
        """
        노선의 정류장을 순서대로 돌려준다.

        :param line: 노선 (예: "2")
        :param city: 도시 코드. 도시마다 같은 노선 이름이 있을 수 있다
        """
        # 1. 순서대로 조회
        stmt = (
            select(Station)
            .where(Station.city == city, Station.line == line)
            .order_by(Station.order_no)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_station_names(self, city: str) -> set[str]:
        """도시의 모든 정류장 이름. 카드 문장 검증용."""
        stmt = select(Station.name).where(Station.city == city)
        return set((await self._session.execute(stmt)).scalars())

    async def get_station(self, station_id: UUID) -> Station | None:
        stmt = select(Station).where(Station.id == station_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_commute_by_user(self, user_id: UUID) -> Commute | None:
        """예외 없이 조회. 데모 카드처럼 없을 수도 있는 자리에서 쓴다."""
        return await self._find_by_user(user_id)

    async def _find_by_user(self, user_id: UUID) -> Commute | None:
        stmt = select(Commute).where(Commute.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()


def get_commute_service(
    session: AsyncSession = Depends(get_db), odsay: OdsayClient = Depends(get_odsay_client)
) -> CommuteService:
    return CommuteService(session, odsay)
