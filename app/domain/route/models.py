from enum import StrEnum
from uuid import UUID, uuid4

from geoalchemy2 import Geometry, WKBElement
from geoalchemy2.shape import to_shape
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Float,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin
from app.core.geo import point_lat, point_lng

PLACE_NAME_MAX_LENGTH = 50
DESCRIPTION_MAX_LENGTH = 200
SEASON_TAG_MAX_LENGTH = 30
PAIR_ID_MAX_LENGTH = 50


class PlaceType(StrEnum):
    """자연 지점의 성격 태그.

    경유지 추림(F-03 ③)·이벤트 점수(F-04)·피드백 가중치(F-16)가 같은 값을 쓴다.
    """

    RIVER = "river"
    PARK = "park"
    TREES = "trees"
    SUNSET = "sunset"
    BRIDGE = "bridge"
    VIEW = "view"


class Place(Base):
    """큐레이션 자연 지점. 팀이 CSV 로 입력하고 scripts/seed_places.py 가 넣는다."""

    __tablename__ = "places"
    __table_args__ = (UniqueConstraint("city", "name", name="uq_places_city_name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(PLACE_NAME_MAX_LENGTH))
    location: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))
    # PlaceType 값의 배열. 검증은 시드(Pydantic)가 한다
    types: Mapped[list[str]] = mapped_column(ARRAY(String(10)))
    # 한 줄 설명. LLM 프롬프트에 그대로 들어간다 (F-05)
    description: Mapped[str] = mapped_column(String(DESCRIPTION_MAX_LENGTH))
    season_tag: Mapped[str | None] = mapped_column(String(SEASON_TAG_MAX_LENGTH), nullable=True)
    # 하천은 진입점·진출점이 한 후보다. 같은 pair_id 두 행이 한 쌍 (F-03)
    pair_id: Mapped[str | None] = mapped_column(String(PAIR_ID_MAX_LENGTH), nullable=True)
    city: Mapped[str] = mapped_column(String(20), index=True)
    # curated = 팀이 CSV 로 넣음, osm = derive_places 가 녹지에서 자동 추출.
    # 자동 지점은 큐레이션 행을 덮어쓰지 않는다
    source: Mapped[str] = mapped_column(
        String(10), default="curated", server_default=text("'curated'")
    )

    @property
    def lat(self) -> float:
        return point_lat(self.location)

    @property
    def lng(self) -> float:
        return point_lng(self.location)

    @property
    def place_types(self) -> list[PlaceType]:
        return [PlaceType(value) for value in self.types]


class GreenKind(StrEnum):
    """green_areas 의 종류. OSM 태그를 #9 적재 SQL 이 이 값으로 바꾼다."""

    PARK = "park"
    FOREST = "forest"
    GRASS = "grass"
    RIVER = "river"
    TREES = "trees"


class GreenArea(Base):
    """OSM 에서 적재한 녹지. 공원·숲·잔디는 면, 하천·가로수열은 선이라 둘 다 받는다 (F-04)."""

    __tablename__ = "green_areas"
    __table_args__ = (
        CheckConstraint(
            "GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON', 'LINESTRING', 'MULTILINESTRING')",
            name="geom_type",
        ),
        UniqueConstraint("osm_id", "kind", name="uq_green_areas_osm_id_kind"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(10))
    # OSM name 태그. 이름 있는 녹지만 자동 Place(derive_places) 후보가 된다
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    geom: Mapped[WKBElement] = mapped_column(Geometry("GEOMETRY", srid=4326))
    # OSM 원본 id. 재적재 때 (osm_id, kind) 로 upsert 한다. 수동 입력은 None
    osm_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    city: Mapped[str] = mapped_column(String(20), index=True)


class LegType(StrEnum):
    """걷는 구간의 종류. 교통수단이 정한다 (F-03 1단계)."""

    WALK_ALL = "walk_all"  # 도보 통근: 출발 → 도착 전체
    FRONT_WALK = "front_walk"  # 대중교통: 출발 → 첫 정류장
    BACK_WALK = "back_walk"  # 대중교통: 마지막 정류장 → 도착
    ALIGHT_EARLY = "alight_early"  # 대중교통: 한 정거장 먼저 내려 → 도착
    AFTER_ARRIVAL = "after_arrival"  # 자동차: 도착 후 반경 10분


class ActionType(StrEnum):
    """오늘 카드의 행동 유형. F-15 단계가 정하고 F-05 LLM 출력 스키마가 같은 값을 쓴다."""

    DETOUR = "detour"  # 돌아가기
    ALIGHT_EARLY = "alight_early"  # 한 정거장 먼저
    DELAY = "delay"  # 늦추기 (곧 노을)
    GLANCE = "glance"  # 고개 돌리기 (기준선에 이벤트)
    AFTER_ARRIVAL = "after_arrival"  # 도착 후
    NONE = "none"  # 오늘 동선엔 자연이 없다


class Route(TimestampMixin, Base):
    """동선 하나의 걷는 구간별 경로. 기준선(최단) 1개 + 경유지 후보 ≤6개.

    점수는 정적 부분(녹지·물가·다리·노을 방향 비율)만 저장한다.
    오늘의 노을 창, 취향별 후보 조건, 최종 score 는 요청 때 RouteService.rank 가 계산한다.
    """

    __tablename__ = "routes"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    commute_id: Mapped[UUID] = mapped_column(
        ForeignKey("commutes.id", ondelete="CASCADE"), index=True
    )
    leg_type: Mapped[LegType] = mapped_column(String(20))
    is_baseline: Mapped[bool] = mapped_column(default=False)
    name: Mapped[str] = mapped_column(String(60))
    minutes: Mapped[float] = mapped_column(Float)
    add_minutes: Mapped[float] = mapped_column(Float)
    polyline: Mapped[WKBElement] = mapped_column(Geometry("LINESTRING", srid=4326))
    # F-04 정적 점수
    green_pct: Mapped[float] = mapped_column(Float)
    water_score: Mapped[float] = mapped_column(Float)
    landmark_score: Mapped[float] = mapped_column(Float)
    sunset_ratio: Mapped[float] = mapped_column(Float)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(10)))
    # 경유한 Place 와 그 한 줄 설명 (LLM 프롬프트 재료)
    spots: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid))
    highlights: Mapped[list[str]] = mapped_column(ARRAY(String(DESCRIPTION_MAX_LENGTH)))
    alight_station_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("stations.id", ondelete="SET NULL"), nullable=True
    )

    @property
    def points(self) -> list[tuple[float, float]]:
        """경로선을 (lat, lng) 리스트로."""
        return [(float(y), float(x)) for x, y in to_shape(self.polyline).coords]
