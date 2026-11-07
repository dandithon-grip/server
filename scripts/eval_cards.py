"""카드 LLM 오프라인 평가 (#61).

실제 LLM 에 상황 24개를 넣고, 서비스와 같은 검증(reject_reason)과 상황별 기대값으로 잰다.
다른 LLM 을 검사관으로 쓰지 않는다. 규칙 검사는 매번 같은 결과를 내기 때문이다.

    uv run python -m scripts.eval_cards                 # 상황당 1회, docs/eval/cards.md 에 저장
    uv run python -m scripts.eval_cards --repeat 3      # 상황당 3회 (일관성)

LLM_API_KEY 가 필요하다. 상황 1개 = LLM 1회 (입력 약 1,700 토큰).
"""

import argparse
import asyncio
import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from app.clients.llm import LlmReply, get_llm_client
from app.core.exceptions import AppException
from app.core.timezone import LOCAL_TZ
from app.domain.card.models import ReasonTag
from app.domain.card.prompt import (
    SYSTEM_PROMPT,
    CandidateInput,
    CardChoice,
    CardInput,
    build_prompt,
    reject_reason,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
# 입력에는 계절 "상태"(절정 여부)가 없다. 이 말을 쓰면 상태를 지어낸 것이다
STAGE_CLAIMS = ("한창", "절정", "만개", "활짝")
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "docs" / "eval" / "cards.md"


class CardLlm(Protocol):
    async def complete_json_reply(
        self, prompt: str, schema: type[BaseModel], *, system: str | None = None
    ) -> LlmReply[BaseModel]: ...


def known_names() -> set[str]:
    """도시의 장소·정류장 이름. 문장에 입력 밖 이름이 있는지 볼 때 쓴다 (DB 없이 CSV 에서)."""
    names: set[str] = set()
    for file, column in (("places_daegu.csv", "name"), ("stations_daegu_line2.csv", "name")):
        with (DATA_DIR / file).open(encoding="utf-8") as handle:
            names.update(row[column] for row in csv.DictReader(handle))
    return names


# --- 상황 -------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    card_input: CardInput
    avoid: frozenset[int] = frozenset()  # 고르면 안 되는 후보 (최근 추천)
    forbid_reasons: frozenset[ReasonTag] = frozenset()  # 근거가 없는 이유 칩
    require_reasons: frozenset[ReasonTag] = frozenset()  # 비어 있지 않으면 이 중 하나
    shortest: bool = False  # 추가 시간이 가장 짧은 후보를 골라야 함


def cand(
    index: int,
    name: str,
    *,
    action: str = "detour",
    add: int = 4,
    green: int = 43,
    event: int = 0,
    score: int = 38,
    tags: tuple[str, ...] = ("park",),
    highlights: tuple[str, ...] = (),
    season: tuple[str, ...] = (),
    station: str | None = None,
    skip: int = 0,
    recent: bool = False,
) -> CandidateInput:
    return CandidateInput(
        index=index,
        name=name,
        action_type=action,
        add_minutes=add,
        green_pct=green,
        event_score=event,
        score=score,
        tags=list(tags),
        highlights=list(highlights),
        season_tags=list(season),
        station=station,
        skip_stops=skip,
        recent=recent,
    )


def inp(
    candidates: list[CandidateInput],
    *,
    leave: str = "18:10",
    sunset: str = "17:24",
    sky: str | None = "clear",
    air: str | None = "good",
    weights: tuple[str, ...] = (),
    level: int = 1,
    window_open: bool = False,
) -> CardInput:
    # 후보 index 를 목록 순서로 다시 매긴다
    ordered = [c.model_copy(update={"index": i}) for i, c in enumerate(candidates)]
    return CardInput(
        candidates=ordered,
        fallback_level=level,
        leave_time=leave,
        sunset_at=sunset,
        sunset_window_open=window_open,
        sky=sky,
        air_grade=air,
        top_tag_weights=list(weights),
    )


PARK = cand(
    0,
    "2.28기념중앙공원 샛길",
    highlights=("반월당 옆 작은 숲. 도심에서 가장 가까운 녹지",),
)
RIVER = cand(
    1,
    "신천 수성교 진입 샛길",
    add=7,
    green=30,
    event=15,
    score=34,
    tags=("river", "trees"),
    highlights=("수성교 옆 계단으로 둔치에 내려서면 물소리가 먼저 들린다",),
    season=("억새",),
)
BOARD = cand(
    2,
    "경대병원까지 걸어가서 타기",
    action="board_late",
    add=6,
    green=35,
    score=26,
    highlights=("국채보상운동기념공원 서쪽 가장자리를 따라 걷는 길",),
    station="경대병원",
    skip=1,
)
SUNSET_RIVER = cand(
    1,
    "신천 상동교 진출 샛길",
    add=9,
    green=28,
    event=25,
    score=40,
    tags=("river", "sunset", "bridge"),
    highlights=("상동교 아래에서 올라오는 길. 퇴근 무렵 서쪽 하늘이 물에 비친다",),
)
NO_SUNSET = frozenset({ReasonTag.SUNSET})

SCENARIOS: list[Scenario] = [
    Scenario("clear_good", "맑음·공기 좋음, 후보 3개", inp([PARK, RIVER, BOARD])),
    Scenario("cloudy_good", "흐림", inp([PARK, RIVER, BOARD], sky="cloudy")),
    Scenario(
        "rain",
        "비. 노을 이유 금지",
        inp([PARK, RIVER, BOARD], sky="rain"),
        forbid_reasons=NO_SUNSET,
    ),
    Scenario(
        "snow",
        "눈. 노을 이유 금지",
        inp([PARK, RIVER, BOARD], sky="snow"),
        forbid_reasons=NO_SUNSET,
    ),
    Scenario(
        "bad_air",
        "미세먼지 나쁨. 가장 짧은 길",
        inp([PARK, RIVER, BOARD], air="bad"),
        shortest=True,
    ),
    Scenario(
        "very_bad_air",
        "미세먼지 매우 나쁨. 가장 짧은 길",
        inp([PARK, RIVER, BOARD], air="very_bad"),
        shortest=True,
    ),
    Scenario(
        "no_weather",
        "날씨·미세먼지 정보 없음. 노을·공기 이유 금지",
        inp([PARK, RIVER, BOARD], sky=None, air=None),
        forbid_reasons=frozenset({ReasonTag.SUNSET, ReasonTag.AIR}),
    ),
    Scenario(
        "sunset_window_clear",
        "일몰 6분 뒤 퇴근, 맑음, 노을 하천 후보 1위. 노을 이유",
        inp([SUNSET_RIVER, PARK, BOARD], leave="17:30", window_open=True),
        require_reasons=NO_SUNSET,
    ),
    Scenario(
        "sunset_window_rain",
        "일몰 직후지만 비. 노을 이유 금지",
        inp([SUNSET_RIVER, PARK, BOARD], leave="17:30", sky="rain"),
        forbid_reasons=NO_SUNSET,
    ),
    Scenario(
        "after_sunset_night",
        "일몰 3시간 뒤 퇴근. 노을 이유 금지",
        inp([PARK, RIVER, BOARD], leave="21:00"),
        forbid_reasons=NO_SUNSET,
    ),
    Scenario(
        "top_recent",
        "1위가 최근 추천. 피해야 함",
        inp([PARK.model_copy(update={"recent": True}), RIVER, BOARD]),
        avoid=frozenset({0}),
    ),
    Scenario(
        "two_recent",
        "1·2위가 최근 추천. 3위를 골라야 함",
        inp(
            [
                PARK.model_copy(update={"recent": True}),
                RIVER.model_copy(update={"recent": True}),
                BOARD,
            ]
        ),
        avoid=frozenset({0, 1}),
    ),
    Scenario(
        "all_recent",
        "전부 최근 추천. 다시 골라도 됨",
        inp([c.model_copy(update={"recent": True}) for c in (PARK, RIVER, BOARD)]),
    ),
    Scenario("single_candidate", "후보 1개", inp([PARK])),
    Scenario("two_candidates", "후보 2개", inp([PARK, RIVER])),
    Scenario(
        "season_tag_top",
        "1위에 단풍 계절 태그",
        inp([PARK.model_copy(update={"season_tags": ["단풍"]}), RIVER, BOARD]),
    ),
    Scenario(
        "alight_early_top",
        "1위가 한 정거장 먼저 내리기",
        inp(
            [
                cand(
                    0,
                    "삼덕교회에서 내려 걷기",
                    action="alight_early",
                    add=1,
                    green=40,
                    score=39,
                    station="삼덕교회",
                    skip=1,
                ),
                PARK,
            ]
        ),
    ),
    Scenario(
        "board_late_top",
        "1위가 걸어가서 타기",
        inp([BOARD.model_copy(update={"score": 40, "green_pct": 45}), PARK, RIVER]),
    ),
    Scenario(
        "three_stops",
        "3정거장 걸어가서 타기",
        inp(
            [
                cand(
                    0,
                    "국채보상운동기념공원 샛길 (3정거장 걸어가서)",
                    action="board_late",
                    add=13,
                    green=62,
                    score=42,
                    highlights=("동성로 옆 넓은 잔디와 느티나무길",),
                    station="방천시장(김광석길)건너",
                    skip=3,
                ),
                PARK,
            ]
        ),
    ),
    Scenario(
        "after_arrival_level3",
        "대체 3단계, 도착 후 후보 1개",
        inp(
            [
                cand(
                    0,
                    "2.28기념중앙공원 샛길 (도착 후)",
                    action="after_arrival",
                    add=4,
                    green=50,
                    score=44,
                    highlights=("반월당 옆 작은 숲. 도심에서 가장 가까운 녹지",),
                )
            ],
            level=3,
        ),
    ),
    Scenario(
        "weights_favor_river",
        "사용자가 하천을 좋아함, 1위는 공원. 바꾸면 deviation",
        inp([PARK, RIVER, BOARD], weights=("river",)),
    ),
    Scenario(
        "generic_names_trap",
        "이름 없는 후보. 근처 유명 장소(수성못·두류공원)를 지어내면 안 됨",
        inp(
            [
                cand(0, "공원 서쪽 입구 샛길", highlights=("공원 서쪽 가장자리를 따라 걷는 길",)),
                cand(1, "놀이터 샛길", add=6, green=40, score=31),
            ]
        ),
    ),
    Scenario(
        "high_event_river_top",
        "물가 이벤트 높은 하천 후보가 1위",
        inp(
            [
                RIVER.model_copy(update={"score": 45, "event_score": 25}),
                PARK,
                BOARD,
            ]
        ),
    ),
    Scenario(
        "close_scores",
        "점수 차가 1점 안",
        inp(
            [
                PARK,
                RIVER.model_copy(update={"score": 38}),
                BOARD.model_copy(update={"score": 37}),
            ]
        ),
    ),
]


# --- 평가 -------------------------------------------------------------------


@dataclass
class Row:
    scenario: str
    choice: int | None = None
    reason_tag: str | None = None
    text: str = ""
    deviation: str | None = None
    rejected: str | None = None  # 서비스 검증에서 거부된 이유 (그러면 카드는 템플릿)
    broken: list[str] = field(default_factory=list)  # 어긴 기대값
    error: str | None = None  # LLM 호출 실패
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    @property
    def accepted(self) -> bool:
        return self.error is None and self.rejected is None

    @property
    def ok(self) -> bool:
        return self.accepted and not self.broken


@dataclass
class Result:
    rows: list[Row]

    def _rate(self, rows: list[Row]) -> float:
        return len(rows) / len(self.rows) if self.rows else 0.0

    @property
    def validation_pass_rate(self) -> float:
        return self._rate([r for r in self.rows if r.accepted])

    @property
    def expectation_pass_rate(self) -> float:
        return self._rate([r for r in self.rows if r.ok])

    @property
    def non_top_rate(self) -> float:
        return self._rate([r for r in self.rows if r.accepted and r.choice not in (None, 0)])

    def _avg(self, values: list[int]) -> int:
        return round(sum(values) / len(values)) if values else 0

    @property
    def avg_input_tokens(self) -> int:
        return self._avg([r.input_tokens for r in self.rows if r.error is None])

    @property
    def avg_output_tokens(self) -> int:
        return self._avg([r.output_tokens for r in self.rows if r.error is None])

    @property
    def avg_latency_ms(self) -> int:
        return self._avg([r.latency_ms for r in self.rows if r.error is None])


def broken_rules(scenario: Scenario, choice: CardChoice) -> list[str]:
    """상황별 기대값과 시스템 프롬프트 규칙 중 어긴 것."""
    broken: list[str] = []
    if choice.choice in scenario.avoid:
        broken.append("avoid_recent")
    if choice.reason_tag in scenario.forbid_reasons:
        broken.append("forbidden_reason")
    if scenario.require_reasons and choice.reason_tag not in scenario.require_reasons:
        broken.append("required_reason")
    candidates = scenario.card_input.candidates
    if scenario.shortest and 0 <= choice.choice < len(candidates):
        fewest = min(c.add_minutes for c in candidates)
        if candidates[choice.choice].add_minutes != fewest:
            broken.append("shortest_on_bad_air")
    # 규칙 3: 1위가 아니면 이유를 쓰고, 1위면 비운다
    if choice.choice != 0 and not choice.deviation:
        broken.append("deviation_when_not_top")
    if choice.choice == 0 and choice.deviation:
        broken.append("no_deviation_when_top")
    # 규칙 1: 입력에 없는 계절 상태를 단정하지 않는다
    if any(word in choice.text for word in STAGE_CLAIMS):
        broken.append("stage_claim")
    # 규칙 5: 숫자는 입력값 그대로. 문장 속 숫자가 입력 어딘가에 있어야 한다
    allowed = set(re.findall(r"\d+", build_prompt(scenario.card_input)))
    if any(number not in allowed for number in re.findall(r"\d+", choice.text)):
        broken.append("numbers_from_input")
    return broken


async def evaluate(scenarios: list[Scenario], llm: CardLlm, *, repeat: int = 1) -> Result:
    """상황마다 LLM 을 repeat 번 부르고 검증·기대값을 잰다. 한 번 실패해도 계속 간다."""
    names = known_names()
    rows: list[Row] = []
    for scenario in scenarios:
        for _ in range(repeat):
            row = Row(scenario=scenario.name)
            try:
                reply = await llm.complete_json_reply(
                    build_prompt(scenario.card_input), CardChoice, system=SYSTEM_PROMPT
                )
            except AppException as exc:
                row.error = exc.code.name
                rows.append(row)
                continue
            choice = CardChoice.model_validate(reply.parsed)
            row.choice, row.reason_tag = choice.choice, str(choice.reason_tag)
            row.text, row.deviation = choice.text, choice.deviation
            row.input_tokens, row.output_tokens = reply.input_tokens, reply.output_tokens
            row.latency_ms = reply.latency_ms
            row.rejected = reject_reason(choice, scenario.card_input, names)
            if row.rejected is None:
                row.broken = broken_rules(scenario, choice)
            rows.append(row)
    return Result(rows)


def render_markdown(result: Result, scenarios: list[Scenario], *, model: str) -> str:
    description = {s.name: s.description for s in scenarios}
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 카드 LLM 오프라인 평가 (#61)",
        "",
        f"측정 {now} · 모델 `{model}` · 호출 {len(result.rows)}회. "
        "`uv run python -m scripts.eval_cards` 로 다시 만든다.",
        "",
        "| 지표 | 값 |",
        "| --- | --- |",
        f"| 서비스 검증 통과 (LLM 카드 그대로 사용) | {result.validation_pass_rate:.0%} |",
        f"| 검증 + 상황별 기대값 모두 충족 | {result.expectation_pass_rate:.0%} |",
        f"| 점수 1위가 아닌 후보 선택 | {result.non_top_rate:.0%} |",
        f"| 평균 입력 / 출력 토큰 | {result.avg_input_tokens} / {result.avg_output_tokens} |",
        f"| 평균 응답 시간 | {result.avg_latency_ms / 1000:.1f}초 |",
        "",
        "| 상황 | 설명 | 선택 | 이유 칩 | 결과 | 문장 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in result.rows:
        if row.error:
            verdict = f"호출 실패 {row.error}"
        elif row.rejected:
            verdict = f"검증 거부 {row.rejected}"
        elif row.broken:
            verdict = "기대 위반 " + ", ".join(row.broken)
        else:
            verdict = "통과"
        text = row.text.replace("|", "/")
        lines.append(
            f"| {row.scenario} | {description[row.scenario]} | {row.choice} | "
            f"{row.reason_tag} | {verdict} | {text} |"
        )
    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser(description="카드 LLM 오프라인 평가")
    parser.add_argument("--repeat", type=int, default=1, help="상황당 호출 횟수")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="결과 마크다운 경로")
    args = parser.parse_args()

    llm = get_llm_client()
    result = await evaluate(SCENARIOS, llm, repeat=args.repeat)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_markdown(result, SCENARIOS, model=llm.model), encoding="utf-8")
    print(
        f"검증 통과 {result.validation_pass_rate:.0%}"
        f" · 기대값 충족 {result.expectation_pass_rate:.0%}"
        f" · 1위 아닌 선택 {result.non_top_rate:.0%} · 입력 {result.avg_input_tokens} 토큰"
        f" · {result.avg_latency_ms / 1000:.1f}초 → {args.out}"
    )


if __name__ == "__main__":
    asyncio.run(main())
