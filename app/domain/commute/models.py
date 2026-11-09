from datetime import datetime, time
from enum import StrEnum
from uuid import UUID, uuid4

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin
from app.core.geo import point_lat, point_lng

AREA_MAX_LENGTH = 50
LINE_MAX_LENGTH = 30  # "대구 2호선", "급행1" 같은 ODsay 노선 이름
STATION_NAME_MAX_LENGTH = 50
EXTERNAL_ID_MAX_LENGTH = 20  # ODsay stationID


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
    """지하철·버스 정류장. 데모 2호선은 시드로 넣고, 그 외는 ODsay 조회 결과를 저장한다.

    ODsay 정류장은 stationID 로 구분한다. 길 건너편 같은 이름 정류장(반대 방향)은 다른 행이다.
    시드 정류장은 stationID 가 없어 지금처럼 (도시, 노선, 이름) 으로 구분한다.
    """

    __tablename__ = "stations"
    __table_args__ = (
        UniqueConstraint("city", "line", "external_id", name="uq_stations_city_line_external"),
        Index(
            "uq_stations_seed_city_line_name",
            "city",
            "line",
            "name",
            unique=True,
            postgresql_where=text("external_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    line: Mapped[str] = mapped_column(String(LINE_MAX_LENGTH))
    name: Mapped[str] = mapped_column(String(STATION_NAME_MAX_LENGTH))
    location: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))
    # 시드 노선 안에서의 순서. 시드 정류장의 "한 정거장 먼저 내리기"는 이 값 ±1 이다.
    # ODsay 정류장은 저장한 구간 안의 순서일 뿐이라 노선 전체 순서로 쓰지 않는다
    order_no: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(EXTERNAL_ID_MAX_LENGTH), nullable=True)
    city: Mapped[str] = mapped_column(String(20), index=True)

    @property
    def lat(self) -> float:
        return point_lat(self.location)

    @property
    def lng(self) -> float:
        return point_lng(self.location)


class RoutesState(StrEnum):
    """샛길 생성 진행 상태 (#97). 생성은 수십 초라 백그라운드로 돌리고 프론트가 상태를 본다."""

    IDLE = "idle"  # 아직 만든 적 없음
    PENDING = "pending"  # 만드는 중
    READY = "ready"  # 경로 있음
    FAILED = "failed"  # 마지막 생성 실패 (또는 오래된 pending)


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
    # 1~3 정거장 먼저 내릴 정류장 (마지막 탑승 구간, 하차역에서 가까운 순)과
    # 1~3 정거장 걸어가서 탈 정류장 (첫 탑승 구간, 탑승역에서 가까운 순).
    # 저장 시점에 정한다 (#44 #51).
    # 다른 사용자의 저장이 바꾸지 못하게 순번 대신 id 로 둔다. 구간 끝 정류장은 들어가지 않는다
    alight_early_station_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(Uuid), default=list, server_default="{}"
    )
    board_late_station_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(Uuid), default=list, server_default="{}"
    )
    preference: Mapped[Preference] = mapped_column(String(10), default=Preference.FULL)
    # 샛길 생성 상태 (#97)
    routes_state: Mapped[str] = mapped_column(
        String(10), default=RoutesState.IDLE.value, server_default=RoutesState.IDLE.value
    )
    routes_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 평소 타는 노선(탑승 계획) ≤3 (#88). order_no 0 이 기본, 위 station_ids·line 은 그 거울
    rides: Mapped[list["CommuteRide"]] = relationship(
        back_populates="commute",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="CommuteRide.order_no",
    )

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


MAX_RIDES = 3


class CommuteRide(Base):
    """동선의 탑승 계획 하나 (#88). "402 를 타면 동대구역 승차 → 달서교회건너 하차".

    걷는 구간·샛길 후보는 계획마다 미리 만들어 두고, 매일은 그중에서 고른다.
    승차·하차 이름은 저장 때 사용자가 본 이름을 그대로 둔다 (정류장 행이 바뀌어도 문구가 안정적).
    """

    __tablename__ = "commute_rides"
    __table_args__ = (UniqueConstraint("commute_id", "order_no", name="uq_commute_rides_order"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    commute_id: Mapped[UUID] = mapped_column(
        ForeignKey("commutes.id", ondelete="CASCADE"), index=True
    )
    order_no: Mapped[int] = mapped_column(Integer)
    line: Mapped[str] = mapped_column(String(LINE_MAX_LENGTH))
    # [탑승, 하차, 탑승, 하차, ...] 환승이면 구간마다 한 쌍
    station_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), default=list)
    alight_early_station_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), default=list)
    board_late_station_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), default=list)
    board_name: Mapped[str] = mapped_column(String(STATION_NAME_MAX_LENGTH))
    alight_name: Mapped[str] = mapped_column(String(STATION_NAME_MAX_LENGTH))
    # 구간별 정류장 id 전부, 저장한 순서 (#93). 탑승 조각을 그릴 때 order_no 추정 대신 이것을 쓴다.
    # 시드 동선(ODsay 없음)은 None 이라 노선 순번으로 채운다
    stop_ids: Mapped[list[list[str]] | None] = mapped_column(JSONB, nullable=True)

    commute: Mapped[Commute] = relationship(back_populates="rides")
