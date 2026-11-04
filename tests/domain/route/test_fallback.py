"""F-15 대체 로직 기대 동작. DB 없이 rank 결과를 만들어 단계 판정만 본다."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from geoalchemy2.shape import from_shape
from shapely.geometry import LineString

from app.domain.commute.models import Preference, TransportMode
from app.domain.route.fallback import decide_fallback
from app.domain.route.models import ActionType, LegType, PlaceType, Route
from app.domain.route.service import RankedRoutes, RouteService

KST = timezone(timedelta(hours=9))
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=KST)
# 11/10 대구 일몰 17:22. 16:50 은 창 밖(32분 전), 10분 뒤 17:00 은 창 안
BEFORE_SUNSET = datetime(2026, 11, 10, 16, 50, tzinfo=KST)
EAST = [(128.6000, 35.8600), (128.6110, 35.8600)]
WEST = list(reversed(EAST))


def route(
    leg_type: LegType,
    *,
    is_baseline: bool = False,
    add_minutes: float = 3,
    green_pct: float = 0,
    landmark_score: float = 0,
    sunset_ratio: float = 0,
    west: bool = False,
) -> Route:
    return Route(
        id=uuid4(),
        commute_id=uuid4(),
        leg_type=leg_type,
        is_baseline=is_baseline,
        name="테스트",
        minutes=10 + add_minutes,
        add_minutes=add_minutes,
        polyline=from_shape(LineString(WEST if west else EAST), srid=4326),
        green_pct=green_pct,
        water_score=0,
        landmark_score=landmark_score,
        sunset_ratio=sunset_ratio,
        tags=[PlaceType.BRIDGE.value] if landmark_score else [],
        spots=[],
        highlights=[],
    )


def decide(
    routes: list[Route],
    *,
    transport_mode: TransportMode = TransportMode.TRANSIT,
    at: datetime = NOON,
    is_clear: bool | None = None,
) -> tuple[int, ActionType, RankedRoutes]:
    service = RouteService.__new__(RouteService)
    ranked = service.rank(routes, preference=Preference.FULL, at=at, is_clear=is_clear)
    result = decide_fallback(ranked, transport_mode=transport_mode, at=at, is_clear=is_clear)
    return result.level, result.action_type, ranked


BASELINES = [
    route(LegType.FRONT_WALK, is_baseline=True),
    route(LegType.BACK_WALK, is_baseline=True),
    route(LegType.ALIGHT_EARLY, is_baseline=True),
]


def test_walking_candidate_is_level_1_detour() -> None:
    level, action, _ = decide([*BASELINES, route(LegType.BACK_WALK, green_pct=40)])

    assert (level, action) == (1, ActionType.DETOUR)


def test_only_alight_early_candidate_is_level_2() -> None:
    routes = [*BASELINES, route(LegType.ALIGHT_EARLY, green_pct=40)]

    level, action, _ = decide(routes)

    assert (level, action) == (2, ActionType.ALIGHT_EARLY)


def test_alight_early_candidate_on_walk_commute_is_not_level_2() -> None:
    # transit 이 아니면 2단계 없음. 후보가 그 구간뿐이라 5단계로 내려간다
    routes = [*BASELINES, route(LegType.ALIGHT_EARLY, green_pct=40)]

    level, action, _ = decide(routes, transport_mode=TransportMode.WALK)

    assert (level, action) == (5, ActionType.NONE)


def test_baseline_with_event_12_is_level_3_glance() -> None:
    routes = [
        route(LegType.FRONT_WALK, is_baseline=True),
        route(LegType.BACK_WALK, is_baseline=True, landmark_score=10, sunset_ratio=0),
        route(LegType.BACK_WALK, add_minutes=20, green_pct=90),  # 상한 초과라 후보 아님
    ]

    level, action, _ = decide(routes)

    assert (level, action) == (3, ActionType.GLANCE)


def test_baseline_facing_sunset_that_opens_in_10_minutes_is_level_3_delay() -> None:
    sunset_baseline = route(
        LegType.BACK_WALK, is_baseline=True, landmark_score=10, sunset_ratio=1.0, west=True
    )

    level, action, _ = decide([sunset_baseline], at=BEFORE_SUNSET, is_clear=True)

    assert (level, action) == (3, ActionType.DELAY)


def test_sunset_delay_needs_clear_sky() -> None:
    sunset_baseline = route(
        LegType.BACK_WALK, is_baseline=True, landmark_score=10, sunset_ratio=1.0, west=True
    )

    level, action, _ = decide([sunset_baseline], at=BEFORE_SUNSET, is_clear=False)

    assert (level, action) == (3, ActionType.GLANCE)


def test_only_after_arrival_candidate_is_level_4() -> None:
    routes = [*BASELINES, route(LegType.AFTER_ARRIVAL, add_minutes=7, landmark_score=10)]

    level, action, ranked = decide(routes)

    assert (level, action) == (4, ActionType.AFTER_ARRIVAL)
    assert len(ranked.eligible) == 1


def test_nothing_is_level_5_none_with_shortest() -> None:
    level, action, ranked = decide(BASELINES)

    assert (level, action) == (5, ActionType.NONE)
    assert len(ranked.shortest) == 3


def test_level_1_wins_over_alight_early_even_if_scored_lower() -> None:
    routes = [
        *BASELINES,
        route(LegType.ALIGHT_EARLY, green_pct=90),
        route(LegType.FRONT_WALK, green_pct=40),
    ]

    level, action, _ = decide(routes)

    assert (level, action) == (1, ActionType.DETOUR)
