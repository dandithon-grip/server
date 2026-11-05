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
from app.domain.commute.schemas import SaveCommuteRequest, TransitLegInput


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
        line = request.line if is_transit else None
        if is_transit and request.transit_legs:
            # 고른 경로의 탑승 구간들을 stations 에 넣고 구간마다 탑승·하차 역 id 를 순서대로
            station_ids, line = await self._store_transit_legs(request.transit_legs, city)
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

        # 3. upsert. user_id unique 충돌이면 모든 값을 갱신한다
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
            "preference": request.preference.value,
        }
        upsert_stmt = insert(Commute).values(**values)
        upsert_stmt = upsert_stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={key: value for key, value in values.items() if key != "user_id"},
        )
        await self._session.execute(upsert_stmt)

        # 4. 저장된 행을 읽고 commit
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
    ) -> tuple[list[UUID], str]:
        """탑승 구간의 정류장을 upsert 한다.

        [탑승, 하차, 탑승, 하차, ...] 순서의 id 목록과 마지막 노선을 돌려준다.
        """
        station_ids: list[UUID] = []
        for leg in legs:
            values = [
                {
                    "city": city,
                    "line": leg.line,
                    "name": stop.name,
                    "order_no": order,
                    "location": make_point(stop.lat, stop.lng),
                }
                for order, stop in enumerate(leg.stops)
            ]
            upsert = insert(Station).values(values)
            upsert = upsert.on_conflict_do_update(
                constraint="uq_stations_city_line_name",
                set_={"order_no": upsert.excluded.order_no, "location": upsert.excluded.location},
            )
            await self._session.execute(upsert)
            for stop in (leg.stops[0], leg.stops[-1]):
                stmt = select(Station.id).where(
                    Station.city == city, Station.line == leg.line, Station.name == stop.name
                )
                station_ids.append((await self._session.execute(stmt)).scalar_one())
        return station_ids, legs[-1].line

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
