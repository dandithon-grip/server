"""F-15 대체 로직 5단계.

후보가 없을 때 빈 화면 대신 무엇을 보여줄지 정한다. 위에서 아래로 처음 맞는 단계가 오늘의 카드다.
DB 를 읽지 않는다. 입력은 RouteService.rank 의 결과다.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.commute.models import TransportMode
from app.domain.route.models import ActionType, LegType
from app.domain.route.scoring import TAG_RATIO_THRESHOLD, sunset_window
from app.domain.route.service import MAX_CANDIDATES, RankedRoute, RankedRoutes

BASELINE_EVENT_MIN = 10  # 3단계: 기준선에 이벤트가 이만큼 있으면 늦추기·고개 돌리기
DELAY_WINDOW = timedelta(minutes=10)  # 이만큼 늦췄을 때 노을 창이 열리면 늦추기
WALK_LEGS = {LegType.WALK_ALL, LegType.FRONT_WALK, LegType.BACK_WALK}


@dataclass(frozen=True)
class FallbackResult:
    level: int
    action_type: ActionType
    candidates: list[RankedRoute]  # 그 단계의 후보 (≤3). 3·5단계는 비어 있다
    shortest: list[RankedRoute]


def decide_fallback(
    ranked: RankedRoutes,
    *,
    transport_mode: TransportMode,
    at: datetime,
    is_clear: bool | None,
) -> FallbackResult:
    """
    대체 단계를 정한다.

    :param ranked: 오늘 조건을 적용한 경로 (eligible 전부 + 기준선)
    :param transport_mode: 2단계(한 정거장 먼저)는 transit 에서만
    :param at: 걷는 시각 (tz-aware). 3단계의 늦추기 판정
    :param is_clear: 맑음 여부. None 이면 늦추기 없음
    """
    # 1. 걷는 구간 안에 후보 있음 → 돌아가기
    walking = [r for r in ranked.eligible if r.route.leg_type in WALK_LEGS]
    if walking:
        return _result(1, ActionType.DETOUR, walking, ranked)

    # 2. transit 이고 한 정거장 먼저 구간에 후보 있음
    early = [r for r in ranked.eligible if r.route.leg_type == LegType.ALIGHT_EARLY]
    if transport_mode == TransportMode.TRANSIT and early:
        return _result(2, ActionType.ALIGHT_EARLY, early, ranked)

    # 3. 기준선 자체에 이벤트(물가·다리·노을) 가 있음 → 곧 노을이면 늦추기, 아니면 고개 돌리기
    eventful = [r for r in ranked.shortest if r.event_score >= BASELINE_EVENT_MIN]
    if eventful:
        action = (
            ActionType.DELAY if _sunset_opens_soon(eventful, at, is_clear) else ActionType.GLANCE
        )
        return _result(3, action, [], ranked)

    # 4. 도착지 반경 10분 안에 후보 있음 → 도착 후
    after = [r for r in ranked.eligible if r.route.leg_type == LegType.AFTER_ARRIVAL]
    if after:
        return _result(4, ActionType.AFTER_ARRIVAL, after, ranked)

    # 5. 오늘 동선엔 자연이 없다
    return _result(5, ActionType.NONE, [], ranked)


def _sunset_opens_soon(baselines: list[RankedRoute], at: datetime, is_clear: bool | None) -> bool:
    """지금은 닫혀 있지만 10분 뒤에는 열리고, 기준선이 일몰 방향으로 걷는 구간이면 True."""
    for ranked in baselines:
        if ranked.route.sunset_ratio < TAG_RATIO_THRESHOLD:
            continue
        lat, lng = ranked.route.points[0]
        now = sunset_window(at, lat=lat, lng=lng, is_clear=is_clear)
        later = sunset_window(at + DELAY_WINDOW, lat=lat, lng=lng, is_clear=is_clear)
        if not now.is_open and later.is_open:
            return True
    return False


def _result(
    level: int, action: ActionType, routes: list[RankedRoute], ranked: RankedRoutes
) -> FallbackResult:
    return FallbackResult(
        level=level,
        action_type=action,
        candidates=routes[:MAX_CANDIDATES],
        shortest=ranked.shortest,
    )
