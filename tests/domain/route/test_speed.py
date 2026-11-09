"""생성 속도 기대 동작 (#96).

경로 점수 질의(읽기 전용)는 세션을 여러 개 열어 동시에 계산하고, 같은 경로선은 한 번만 계산한다.
도착 후 구간은 퇴근(집 주변)만 만든다 (카드·알림이 퇴근 전용).
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tmap import LatLng
from app.domain.route import service as route_service
from app.domain.route.scoring import ScoringService, StaticScore
from app.domain.route.service import SCORING_CONCURRENCY, RouteService
from tests.domain.route.test_routes import FakeTmap

A = LatLng(lat=35.8600, lng=128.6000)
B = LatLng(lat=35.8600, lng=128.6110)
C = LatLng(lat=35.8610, lng=128.6050)


async def test_scores_are_computed_concurrently_and_deduplicated(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    opened = 0
    active = 0
    peak = 0

    @asynccontextmanager
    async def counting_factory() -> AsyncIterator[AsyncSession]:
        nonlocal opened, active, peak
        opened += 1
        active += 1
        peak = max(peak, active)
        try:
            yield db_session
        finally:
            active -= 1

    async def slow_score(self: ScoringService, polyline: list[LatLng], *, city: str) -> StaticScore:
        await asyncio.sleep(0.05)
        return StaticScore(
            green_pct=float(len(polyline)),
            water_score=0,
            landmark_score=0,
            sunset_ratio=0,
            sunset_ratio_reverse=0,
            tags=[],
        )

    monkeypatch.setattr(route_service, "scoring_session_factory", counting_factory)
    monkeypatch.setattr(ScoringService, "score_static", slow_score)
    service = RouteService(db_session, FakeTmap())  # type: ignore[arg-type]
    polylines = [[A, B], [A, C, B], [A, B], [B, A], [A, C, B], [C, B]]

    started = datetime.now()
    scores = await service._score_many(polylines, "daegu")
    elapsed = (datetime.now() - started).total_seconds()

    assert [s.green_pct for s in scores] == [2, 3, 2, 2, 3, 2]
    assert opened == 4  # 같은 경로선 2개는 다시 계산하지 않는다
    assert peak >= 2  # 동시에 돈다
    assert peak <= SCORING_CONCURRENCY
    assert elapsed < 0.05 * 4  # 순차(0.2초)보다 빠르다


def test_tmap_runs_six_at_a_time() -> None:
    assert route_service.TMAP_CONCURRENCY == 6
