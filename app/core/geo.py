"""좌표 변환 공통 함수. PostGIS POINT(WKB) ↔ 위도·경도."""

from geoalchemy2 import WKBElement
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import Point

SRID_WGS84 = 4326


def make_point(lat: float, lng: float) -> WKBElement:
    """위도·경도를 POINT 컬럼 값으로 만든다. PostGIS 는 (경도, 위도) 순서다."""
    return from_shape(Point(lng, lat), srid=SRID_WGS84)


def point_lat(point: WKBElement) -> float:
    return float(to_shape(point).y)


def point_lng(point: WKBElement) -> float:
    return float(to_shape(point).x)
