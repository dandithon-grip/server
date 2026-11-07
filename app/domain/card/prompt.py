"""F-05 LLM 입력·출력 형식과 프롬프트.

프롬프트 문자열과 출력 스키마는 전부 여기 있다 (명세: LLM 출력 형식·말투는 프롬프트 문자열).
모델은 바꾸지 않는다.
"""

import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.card.models import ReasonTag

TEXT_MAX_CHARS = 80

SYSTEM_PROMPT = """너는 출퇴근길에 자연을 지나가게 돕는 서비스 "샛길"의 카드 작성자다.
서버가 이미 실제 경로의 숫자(추가 시간, 녹지 %, 이벤트 점수)를 계산해 후보를 걸러 놓았다.
너는 후보 중 오늘 걸을 하나를 고르고, 왜 오늘 이 길인지 두 문장으로 쓴다.

규칙
1. 장소·정류장·시각·거리 이름은 입력에 있는 것만 쓴다. 입력에 없는 장소를 지어내지 않는다.
   season_tags 는 그 장소의 계절 풍경일 뿐이다. 지금 한창·절정·만개라고 단정하지 않는다.
2. recent 가 true 인 후보(최근 3일 안에 추천)는 피한다. 다른 후보가 없을 때만 다시 고른다.
3. 점수 1위(index 0)와 다르게 고르면 deviation 에 한 줄 이유를 쓴다. 같으면 null.
4. 이유 칩(reason_tag)은 하나만. 날씨·일몰·계절 상태·미세먼지를 근거로 고른다.
   sunset_window_open 이 true 면 지금이 노을 시간이다. sunset 태그 후보를 우선하고 이유 칩은 sunset.
5. text 는 한국어 두 문장, 80자 이내, 반말 금지, 숫자는 입력값 그대로.
"""


# 행동 유형은 넣지 않는다. 서버가 고른 후보의 구간으로 정한다 (#51).
# 출력 스키마는 입력 토큰으로 청구된다. action_type 하나가 353 토큰이었다 (#59).
# docstring 은 스키마 description 으로 LLM 에 전송되므로 짧게 둔다
class CardChoice(BaseModel):
    """LLM 출력."""

    model_config = ConfigDict(frozen=True)

    choice: int = Field(..., ge=0, description="후보 index. 0 이 점수 1위")
    reason_tag: ReasonTag = Field(..., description="이유 칩 하나")
    text: str = Field(..., min_length=1, max_length=TEXT_MAX_CHARS, description="두 문장")
    deviation: str | None = Field(None, max_length=200, description="1위와 다르면 한 줄 이유")


class CandidateInput(BaseModel):
    """후보 하나. 기본값인 필드(빈 목록·None·0·False)는 프롬프트에서 뺀다 (#59)."""

    model_config = ConfigDict(frozen=True)

    index: int
    name: str
    action_type: str = Field(..., description="detour · alight_early · board_late · after_arrival")
    add_minutes: int
    green_pct: int
    event_score: int
    score: int
    tags: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list, description="경유 지점 한 줄 설명")
    season_tags: list[str] = Field(default_factory=list, description="경유 지점의 계절 상태")
    station: str | None = Field(
        None, description="먼저 내리기면 내릴 정류장, 걸어가서 타기면 탈 정류장"
    )
    skip_stops: int = Field(0, description="건너뛴 정거장 수")
    recent: bool = Field(False, description="최근 3일 안에 추천한 경로")


class CardInput(BaseModel):
    """LLM 에 넘기는 입력 전부. 프롬프트 재현을 위해 JSON 으로 직렬화한다."""

    model_config = ConfigDict(frozen=True)

    candidates: list[CandidateInput]
    fallback_level: int
    leave_time: str
    sunset_at: str
    # 노을 창(일몰 ±30분·맑음). 서버가 계산한다. LLM 이 시각 계산을 하지 않게 (#61 평가)
    sunset_window_open: bool = False
    sky: str | None = None
    air_grade: str | None = None
    top_tag_weights: list[str] = Field(default_factory=list)


def build_prompt(card_input: CardInput) -> str:
    """입력을 압축 JSON 으로. 들여쓰기·공백·기본값 필드를 빼서 토큰을 줄인다 (#59)."""
    payload = card_input.model_dump(exclude_defaults=True)
    return (
        "아래 입력만 보고 오늘의 카드를 만들어라. 후보는 점수순이고 index 0 이 1위다.\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def names_in_input(card_input: CardInput) -> set[str]:
    """입력에 등장한 장소·정류장 이름. LLM 문장에 이 밖의 이름이 있으면 폴백."""
    names: set[str] = set()
    for candidate in card_input.candidates:
        names.add(candidate.name)
        names.update(candidate.highlights)
        if candidate.station:
            names.add(candidate.station)
    return names


def reject_reason(choice: CardChoice, card_input: CardInput, known_names: set[str]) -> str | None:
    """
    LLM 출력을 받을지 정한다. 서비스와 오프라인 평가(scripts/eval_cards.py)가 같은 규칙을 쓴다.

    :param known_names: 도시의 장소·정류장 이름 전부. 문장에 입력 밖 이름이 있는지 볼 때 쓴다
    :return: 받으면 None, 아니면 이유 ("choice_out_of_range" · "foreign_names:이름,...")
    """
    # 1. 후보 범위
    if not 0 <= choice.choice < len(card_input.candidates):
        return "choice_out_of_range"
    # 2. 입력에 없는 장소·정류장 이름. 입력 문자열 어디에든 들어 있는 이름은 입력에 있는 것이다
    #    (예: 경로 "국채보상운동기념공원 북쪽 입구 샛길" 에 대해 "국채보상운동기념공원")
    allowed_text = " ".join(names_in_input(card_input))
    foreign = sorted(
        name for name in known_names if name and name in choice.text and name not in allowed_text
    )
    if foreign:
        return "foreign_names:" + ",".join(foreign)
    return None


def format_time(value: datetime) -> str:
    return value.strftime("%H:%M")
