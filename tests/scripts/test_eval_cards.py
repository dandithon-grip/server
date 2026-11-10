"""카드 LLM 오프라인 평가 스크립트 기대 동작 (#61). 실제 LLM 대신 가짜를 쓴다."""

from dataclasses import dataclass, field

from pydantic import BaseModel

from app.clients.llm import LlmReply
from app.domain.card.models import ReasonTag
from app.domain.card.prompt import CardChoice
from scripts.eval_cards import SCENARIOS, Scenario, evaluate, known_names

DEFAULT = CardChoice(
    choice=0, reason_tag=ReasonTag.PARK, text="오늘은 가까운 숲길로 걸어 보세요.", deviation=None
)


@dataclass
class ScriptedLlm:
    """정해 둔 출력 하나를 돌려준다. 없으면 0번 + 무난한 문장."""

    answer: CardChoice = field(default_factory=lambda: DEFAULT)

    async def complete_json_reply(
        self, prompt: str, schema: type[BaseModel], *, system: str | None = None
    ) -> LlmReply[BaseModel]:
        return LlmReply(
            parsed=self.answer,
            input_tokens=1700,
            output_tokens=90,
            latency_ms=2000,
        )


def scenario(name: str) -> Scenario:
    return next(s for s in SCENARIOS if s.name == name)


def test_there_are_30_scenarios_with_unique_names() -> None:
    # #108: 운영에서 생기는데 평가에 없던 상황 6개를 더했다
    assert len(SCENARIOS) == 30
    assert len({s.name for s in SCENARIOS}) == 30


def test_scenario_candidates_are_score_ordered_like_the_service() -> None:
    # 서비스는 후보를 점수 내림차순으로 넘긴다. 상황도 같아야 1위 이탈률이 의미 있다
    for s in SCENARIOS:
        scores = [c.score for c in s.card_input.candidates]
        assert scores == sorted(scores, reverse=True), s.name


def test_known_names_come_from_curated_places_and_stations() -> None:
    names = known_names()

    assert "수성못" in names  # 입력에 없는 유명 장소 함정에 쓴다
    assert "반월당" in names


async def test_inventing_a_place_fails_validation() -> None:
    trap = scenario("clear_good")
    llm = ScriptedLlm(
        CardChoice(
            choice=0,
            reason_tag=ReasonTag.PARK,
            text="수성못까지 걸어 보세요. 공기가 좋아요.",
            deviation=None,
        )
    )

    result = await evaluate([trap], llm)

    assert result.rows[0].rejected == "foreign_names:수성못"
    assert result.validation_pass_rate == 0.0


async def test_choosing_recent_route_fails_the_scenario_expectation() -> None:
    recent = scenario("top_recent")  # 0번이 최근 추천
    llm = ScriptedLlm(
        CardChoice(
            choice=0,
            reason_tag=ReasonTag.PARK,
            text="오늘은 가까운 숲길로 걸어 보세요.",
            deviation=None,
        )
    )

    result = await evaluate([recent], llm)

    assert result.rows[0].rejected is None
    assert "avoid_recent" in result.rows[0].broken


async def test_number_not_in_input_breaks_the_number_rule() -> None:
    base = scenario("clear_good")
    llm = ScriptedLlm(
        CardChoice(
            choice=0,
            reason_tag=ReasonTag.PARK,
            text="99분만 더 걸으면 숲이에요. 맑아요.",
            deviation=None,
        )
    )

    result = await evaluate([base], llm)

    assert "numbers_from_input" in result.rows[0].broken


async def test_non_top_choice_without_deviation_breaks_rule_3() -> None:
    base = scenario("clear_good")
    llm = ScriptedLlm(
        CardChoice(
            choice=1,
            reason_tag=ReasonTag.RIVER,
            text="오늘은 물가로 걸어 보세요. 맑아요.",
            deviation=None,
        )
    )

    result = await evaluate([base], llm)

    assert "deviation_when_not_top" in result.rows[0].broken


async def test_summary_counts_tokens_and_latency() -> None:
    result = await evaluate([scenario("clear_good"), scenario("cloudy_good")], ScriptedLlm())

    assert result.avg_input_tokens == 1700
    assert result.avg_latency_ms == 2000
    assert result.validation_pass_rate == 1.0


async def test_claiming_peak_season_breaks_the_stage_rule() -> None:
    # 입력의 season_tags 는 "억새" 같은 계절 풍경일 뿐, 지금 절정인지는 모른다
    llm = ScriptedLlm(
        CardChoice(
            choice=0,
            reason_tag=ReasonTag.SEASON,
            text="억새가 한창이에요. 4분만 더 걸어 보세요.",
            deviation=None,
        )
    )

    result = await evaluate([scenario("clear_good")], llm)

    assert "stage_claim" in result.rows[0].broken


async def test_plain_form_ending_breaks_the_polite_rule() -> None:
    # 규칙 5: 반말 금지. "좋다." 처럼 끝나면 위반, "좋습니다." "좋아요." 는 통과
    llm = ScriptedLlm(
        CardChoice(
            choice=0,
            reason_tag=ReasonTag.PARK,
            text="오늘은 숲길이 좋다. 4분만 더 걸어 보세요.",
            deviation=None,
        )
    )

    result = await evaluate([scenario("clear_good")], llm)

    assert "polite_ending" in result.rows[0].broken


async def test_polite_endings_pass() -> None:
    llm = ScriptedLlm(
        CardChoice(
            choice=0,
            reason_tag=ReasonTag.PARK,
            text="오늘은 숲길이 좋습니다. 4분만 더 걸어 보세요.",
            deviation=None,
        )
    )

    result = await evaluate([scenario("clear_good")], llm)

    assert "polite_ending" not in result.rows[0].broken


# --- #108 추가 상황 6개 -----------------------------------------------------


async def test_sunset_window_with_bad_air_requires_the_shortest_route() -> None:
    # 노을 창이 열려도 미세먼지가 나쁘면 건강이 먼저다. 노을 하천(추가 9분)을 고르면 어긴 것
    s = scenario("sunset_window_bad_air")
    assert s.shortest and s.card_input.sunset_window_open and s.card_input.air_grade == "bad"
    llm = ScriptedLlm(
        CardChoice(
            choice=0,
            reason_tag=ReasonTag.SUNSET,
            text="노을을 보며 걸어 보세요. 물이 붉어요.",
            deviation=None,
        )
    )

    result = await evaluate([s], llm)

    assert "shortest_on_bad_air" in result.rows[0].broken


async def test_rain_forbids_sunset_reason_even_when_the_user_likes_sunsets() -> None:
    s = scenario("weights_sunset_rain")
    assert "sunset" in s.card_input.top_tag_weights and s.card_input.sky == "rain"
    llm = ScriptedLlm(
        CardChoice(
            choice=0,
            reason_tag=ReasonTag.SUNSET,
            text="노을을 보며 걸어 보세요. 물이 붉어요.",
            deviation=None,
        )
    )

    result = await evaluate([s], llm)

    assert "forbidden_reason" in result.rows[0].broken


def test_same_park_three_entrances_share_the_park_name() -> None:
    # 11/10 데모 카드: 후보 3개가 전부 같은 공원의 다른 입구였다
    s = scenario("same_park_three")
    assert len(s.card_input.candidates) == 3
    assert all("국채보상운동기념공원" in c.name for c in s.card_input.candidates)


async def test_ride_line_label_in_text_is_not_a_foreign_name() -> None:
    # 노선 3개 등록(#88)의 환승 라벨은 입력에 있으므로 문장에 써도 거부하지 않는다
    s = scenario("ride_line_board_late")
    line = s.card_input.candidates[0].line
    assert line and "→" in line
    llm = ScriptedLlm(
        CardChoice(
            choice=0,
            reason_tag=ReasonTag.PARK,
            text=f"{line}을 타기 전에 공원을 지나 보세요. 맑아요.",
            deviation=None,
        )
    )

    result = await evaluate([s], llm)

    assert result.rows[0].rejected is None
    assert "numbers_from_input" not in result.rows[0].broken


def test_car_after_arrival_scenario_is_level_3_with_two_walks() -> None:
    s = scenario("car_after_arrival_two")
    assert s.card_input.fallback_level == 3
    assert [c.action_type for c in s.card_input.candidates] == ["after_arrival", "after_arrival"]


def test_walk_all_cap_candidate_sits_near_the_detour_cap() -> None:
    # 도보 통근 전체 우회. 상한(15분) 바로 아래라 "짧게" 취향이면 빠질 후보
    s = scenario("walk_all_near_cap")
    assert s.card_input.candidates[0].add_minutes == 14
