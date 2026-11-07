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


def test_there_are_24_scenarios_with_unique_names() -> None:
    assert len(SCENARIOS) == 24
    assert len({s.name for s in SCENARIOS}) == 24


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
