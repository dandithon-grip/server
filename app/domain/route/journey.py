"""전체 여정 조립 (#77).

Route 는 걷는 구간 하나다. 프론트가 "출발 → 정류장 → 탑승 → 정류장 → 도착" 을 한 줄로 그릴 수 있게,
후보 하나를 골랐을 때의 전체 여정을 조각(Segment) 순서로 이어 준다. DB 를 읽지 않는다.
정류장 조회는 RouteService.journeys 가 하고, 여기는 순수 조립만 한다.
"""

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from app.clients.tmap import LatLng
from app.domain.commute.models import Station, TransportMode
from app.domain.route.models import Direction, LegType

if TYPE_CHECKING:
    from app.domain.route.service import RankedRoute

Point = tuple[float, float]  # (lat, lng)


class SegmentKind(StrEnum):
    WALK = "walk"  # 걷기 (TMAP 보행 경로)
    RIDE = "ride"  # 버스·지하철 탑승. 정류장 좌표를 잇는 선이라 실제 노선 모양과 다를 수 있다
    DRIVE = "drive"  # 자동차. 출발·도착을 잇는 직선


class SegmentRole(StrEnum):
    USUAL = "usual"  # 평소 길
    SAETGIL = "saetgil"  # 오늘 바뀐 걷는 구간. 프론트가 강조한다
    AFTER_ARRIVAL = "after_arrival"  # 도착한 뒤 더 걷는 산책 (F-15 3단계)


@dataclass(frozen=True)
class Segment:
    kind: SegmentKind
    role: SegmentRole
    points: list[Point]
    minutes: float | None = None  # 탑승은 실측이 없어 None
    line: str | None = None
    from_name: str | None = None
    to_name: str | None = None


Ride = list[Station]  # 탑승 한 번: [탑승, 중간..., 하차] 진행 방향 순서


def orient_rides(rides: list[Ride], direction: Direction) -> list[Ride]:
    """출근 방향으로 만든 탑승 목록을 걷는 방향으로. 퇴근이면 순서와 정류장 둘 다 뒤집는다."""
    if direction == Direction.TO_WORK:
        return rides
    return [list(reversed(ride)) for ride in reversed(rides)]


def compose_journey(
    ranked: "RankedRoute",
    *,
    shortest: dict[LegType, "RankedRoute"],
    rides: list[Ride],
    transport_mode: TransportMode,
    trip: tuple[LatLng, LatLng],
) -> list[Segment]:
    """
    경로 하나를 골랐을 때의 전체 여정.

    :param ranked: 걷는 방향으로 본 경로 (후보 또는 기준선)
    :param shortest: 걷는 방향 구간별 기준선. 후보가 바꾸지 않는 구간은 이걸로 채운다
    :param rides: 걷는 방향 탑승 목록. 대중교통이 아니면 빈 목록
    :param transport_mode: 교통수단. 평소 여정의 모양을 정한다
    :param trip: 걷는 방향의 (출발, 도착). 자동차 직선에 쓴다
    :return: 끊김 없이 이어지는 조각 목록. 기준선이 없는 구간은 비운다
    """
    leg = ranked.leg_type

    # 1. 도착 후 산책: 평소 여정을 다 간 뒤에 붙는다
    if leg == LegType.AFTER_ARRIVAL:
        usual = _usual_journey(shortest, rides, transport_mode, trip)
        return connect([*usual, _walk(ranked, SegmentRole.AFTER_ARRIVAL)])

    role = SegmentRole.USUAL if ranked.route.is_baseline else SegmentRole.SAETGIL

    # 2. 도보 통근: 조각 하나
    if leg == LegType.WALK_ALL:
        return [_walk(ranked, role)]

    # 3. 대중교통: 후보가 바꾸는 쪽만 후보로, 나머지는 기준선·탑승으로
    front = shortest.get(LegType.FRONT_WALK)
    back = shortest.get(LegType.BACK_WALK)
    head: Segment | None = _walk(front, SegmentRole.USUAL) if front else None
    tail: Segment | None = _walk(back, SegmentRole.USUAL) if back else None
    if leg == LegType.FRONT_WALK:
        head = _walk(ranked, role)
    elif leg == LegType.BACK_WALK:
        tail = _walk(ranked, role)
    elif leg == LegType.BOARD_LATE:
        head = _walk(ranked, SegmentRole.SAETGIL)
        rides = _trim_start(rides, ranked.board_station_id)
    elif leg == LegType.ALIGHT_EARLY:
        tail = _walk(ranked, SegmentRole.SAETGIL)
        rides = _trim_end(rides, ranked.alight_station_id)

    segments = [head] if head else []
    segments += [_ride(ride) for ride in rides]
    if tail:
        segments.append(tail)
    return connect(segments)


def connect(segments: list[Segment]) -> list[Segment]:
    """조각 사이 틈을 메운다. TMAP 보행 경로는 도로에 붙어 정류장 좌표와 몇 m 어긋난다.

    연결점은 걷기·자동차 조각에 넣고, 탑승 조각의 정류장 좌표는 그대로 둔다
    (프론트가 탑승 조각의 양 끝을 정류장 점으로 찍는다).
    """
    out: list[Segment] = []
    for segment in segments:
        if out and out[-1].points[-1] != segment.points[0]:
            before = out[-1]
            if segment.kind == SegmentKind.RIDE and before.kind != SegmentKind.RIDE:
                out[-1] = replace(before, points=[*before.points, segment.points[0]])
            else:
                segment = replace(segment, points=[before.points[-1], *segment.points])
        out.append(segment)
    return out


def _usual_journey(
    shortest: dict[LegType, "RankedRoute"],
    rides: list[Ride],
    transport_mode: TransportMode,
    trip: tuple[LatLng, LatLng],
) -> list[Segment]:
    if transport_mode == TransportMode.CAR:
        start, end = trip
        points = [(start.lat, start.lng), (end.lat, end.lng)]
        return [Segment(SegmentKind.DRIVE, SegmentRole.USUAL, points)]
    if transport_mode == TransportMode.WALK:
        whole = shortest.get(LegType.WALK_ALL)
        return [_walk(whole, SegmentRole.USUAL)] if whole else []
    segments: list[Segment] = []
    if front := shortest.get(LegType.FRONT_WALK):
        segments.append(_walk(front, SegmentRole.USUAL))
    segments += [_ride(ride) for ride in rides]
    if back := shortest.get(LegType.BACK_WALK):
        segments.append(_walk(back, SegmentRole.USUAL))
    return segments


def _walk(ranked: "RankedRoute", role: SegmentRole) -> Segment:
    return Segment(SegmentKind.WALK, role, list(ranked.points), minutes=ranked.route.minutes)


def _ride(ride: Ride) -> Segment:
    return Segment(
        SegmentKind.RIDE,
        SegmentRole.USUAL,
        [(s.lat, s.lng) for s in ride],
        line=ride[0].line,
        from_name=ride[0].name,
        to_name=ride[-1].name,
    )


def _trim_start(rides: list[Ride], station_id: UUID | None) -> list[Ride]:
    """첫 탑승을 걸어가서 탈 정류장부터. 정류장이 목록에 없으면(SET NULL) 그대로 둔다."""
    if not rides or station_id is None:
        return rides
    first = rides[0]
    index = next((i for i, s in enumerate(first) if s.id == station_id), None)
    if index is None or len(first) - index < 2:
        return rides
    return [first[index:], *rides[1:]]


def _trim_end(rides: list[Ride], station_id: UUID | None) -> list[Ride]:
    """마지막 탑승을 먼저 내릴 정류장까지. 정류장이 목록에 없으면 그대로 둔다."""
    if not rides or station_id is None:
        return rides
    last = rides[-1]
    index = next((i for i, s in enumerate(last) if s.id == station_id), None)
    if index is None or index < 1:
        return rides
    return [*rides[:-1], last[: index + 1]]
