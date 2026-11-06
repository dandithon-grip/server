"""F-05 LLM 입력·출력 형식과 프롬프트.

프롬프트 문자열과 출력 스키마는 전부 여기 있다 (명세: LLM 출력 형식·말투는 프롬프트 문자열).
모델은 바꾸지 않는다.
"""

import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.card.models import ReasonTag
from app.domain.route.models import ActionType

TEXT_MAX_CHARS = 80

SYSTEM_PROMPT = """너는 출퇴근길에 자연을 지나가게 돕는 서비스 "샛길"의 카드 작성자다.
서버가 이미 실제 경로의 숫자(추가 시간, 녹지 %, 이벤트 점수)를 계산해 후보를 걸러 놓았다.
너는 후보 중 오늘 걸을 하나를 고르고, 왜 오늘 이 길인지 두 문장으로 쓴다.

규칙
1. 장소·정류장·시각·거리 이름은 입력에 있는 것만 쓴다. 입력에 없는 장소를 지어내지 않는다.
2. 최근 3일 안에 추천한 경로(recent_route_ids)는 피한다. 다른 후보가 없을 때만 다시 고른다.
3. 점수 1위(index 0)와 다르게 고르면 deviation 에 한 줄 이유를 쓴다. 같으면 null.
4. 이유 칩(reason_tag)은 하나만. 날씨·일몰·계절 상태·미세먼지를 근거로 고른다.
5. text 는 한국어 두 문장, 80자 이내, 반말 금지, 숫자는 입력값 그대로.
"""


class CardChoice(BaseModel):
    """LLM 출력. 이 스키마로 강제된다."""

    model_config = ConfigDict(frozen=True)

    choice: int = Field(..., ge=0, description="후보 index. 0 이 점수 1위")
    action_type: ActionType = Field(..., description="행동 유형")
    reason_tag: ReasonTag = Field(..., description="이유 칩 하나")
    text: str = Field(..., min_length=1, max_length=TEXT_MAX_CHARS, description="두 문장")
    deviation: str | None = Field(None, max_length=200, description="1위와 다르면 한 줄 이유")


class CandidateInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    route_id: str
    name: str
    leg_type: str
    action_type: str
    add_minutes: int
    green_pct: int
    event_score: int
    score: int
    tags: list[str]
    highlights: list[str] = Field(..., description="경유 지점 한 줄 설명")
    season_tags: list[str] = Field(..., description="경유 지점의 계절 상태")
    station: str | None = Field(
        None, description="먼저 내리기면 내릴 정류장, 늦게 타기면 걸어가서 탈 정류장"
    )
    skip_stops: int = Field(0, description="건너뛴 정거장 수. 걷기 샛길은 0")


class CardInput(BaseModel):
    """LLM 에 넘기는 입력 전부. 프롬프트 재현을 위해 JSON 으로 직렬화한다."""

    model_config = ConfigDict(frozen=True)

    candidates: list[CandidateInput]
    fallback_level: int
    leave_time: str
    sunset_at: str
    sky: str | None
    air_grade: str | None
    top_tag_weights: list[str]
    recent_route_ids: list[str]


def build_prompt(card_input: CardInput) -> str:
    payload = card_input.model_dump()
    return (
        "아래 입력만 보고 오늘의 카드를 만들어라. 후보는 점수순이고 index 0 이 1위다.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )


def format_time(value: datetime) -> str:
    return value.strftime("%H:%M")
