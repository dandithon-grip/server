from datetime import time
from enum import StrEnum
from uuid import UUID, uuid4

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import ForeignKey, Integer, String, Time, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin
from app.core.geo import point_lat, point_lng

AREA_MAX_LENGTH = 50
LINE_MAX_LENGTH = 10
STATION_NAME_MAX_LENGTH = 50


class TransportMode(StrEnum):
    """교통수단. 걷는 구간을 어떻게 자를지 결정한다 (F-03)."""

    WALK = "walk"
    TRANSIT = "transit"
    CAR = "car"


class Preference(StrEnum):
    """기본 취향. 후보 조건값(우회 상한·녹지 기준)을 고른다 (F-03 표)."""

    SHORT = "short"
    FULL = "full"
    SUNSET = "sunset"


class Station(Base):
    """지하철·버스 정류장. 데모 2호선은 시드로 넣고, 그 외는 ODsay 조회 결과를 저장한다."""

    __tablename__ = "stations"
    __table_args__ = (UniqueConstraint("city", "line", "name", name="uq_stations_city_line_name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    line: Mapped[str] = mapped_column(String(LINE_MAX_LENGTH))
    name: Mapped[str] = mapped_column(String(STATION_NAME_MAX_LENGTH))
    location: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))
    # 노선 안에서의 순서. "한 정거장 먼저 내리기"는 이 값 ±1 이다
    order_no: Mapped[int] = mapped_column(Integer)
    city: Mapped[str] = mapped_column(String(20), index=True)

    @property
    def lat(self) -> float:
        return point_lat(self.location)

    @property
    def lng(self) -> float:
        return point_lng(self.location)


class Commute(TimestampMixin, Base):
    """사용자의 출퇴근 동선. 사용자당 1행."""

    __tablename__ = "commutes"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    # 좌표는 경로 계산(F-03)에 쓴다. 본인 응답 외에는 내보내지 않는다
    from_location: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))
    to_location: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))
    from_area: Mapped[str] = mapped_column(String(AREA_MAX_LENGTH))
    to_area: Mapped[str] = mapped_column(String(AREA_MAX_LENGTH))
    go_time: Mapped[time] = mapped_column(Time)
    leave_time: Mapped[time] = mapped_column(Time)
    transport_mode: Mapped[TransportMode] = mapped_column(String(10))
    # transit 일 때만 값이 있다. walk/car 는 None, []
    line: Mapped[str | None] = mapped_column(String(LINE_MAX_LENGTH), nullable=True)
    station_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), default=list)
    preference: Mapped[Preference] = mapped_column(String(10), default=Preference.FULL)

    @property
    def from_lat(self) -> float:
        return point_lat(self.from_location)

    @property
    def from_lng(self) -> float:
        return point_lng(self.from_location)

    @property
    def to_lat(self) -> float:
        return point_lat(self.to_location)

    @property
    def to_lng(self) -> float:
        return point_lng(self.to_location)
