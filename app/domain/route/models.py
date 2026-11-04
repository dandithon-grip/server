from enum import StrEnum
from uuid import UUID, uuid4

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import BigInteger, CheckConstraint, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
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
    geom: Mapped[WKBElement] = mapped_column(Geometry("GEOMETRY", srid=4326))
    # OSM 원본 id. 재적재 때 (osm_id, kind) 로 upsert 한다. 수동 입력은 None
    osm_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    city: Mapped[str] = mapped_column(String(20), index=True)
