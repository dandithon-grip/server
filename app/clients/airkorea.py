"""에어코리아 대기오염정보(시도별 실시간 측정정보) 래퍼.

F-05 가 카드 문장과 홈 헤더에 쓸 미세먼지 등급·농도를 받는다. service 는 httpx 를 직접
부르지 않고 이 클라이언트의 AirQuality 만 본다.

엔드포인트: GET https://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty
- 쿼리 serviceKey(공공데이터포털 인증키)·returnType=json·sidoName(시도명)·ver
- 응답: response.header.resultCode 와 response.body.items[] (한 행 = 측정소 한 곳)
- 등급(pm10Grade·pm25Grade) 1 좋음 / 2 보통 / 3 나쁨 / 4 매우나쁨
- 측정망(mangName) 은 도시대기·도로변대기·국가배경농도·교외대기·항만
근거: 공공데이터포털 활용가이드 '한국환경공단_에어코리아_대기오염정보_기술문서_v1.4'
      (요청·응답 명세, 등급 코드표, 에러코드, 응답 예제)

값 결측은 예외가 아니라 None 필드로 돌려준다. 야간이나 장비 점검 중에는 농도가 '-' 로,
등급이 null 로 오는데 그걸 502 로 만들면 카드 생성 전체가 막힌다 (#6 결정).
"""

import logging
import time
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.core.exceptions import AppException, ErrorCode

logger = logging.getLogger(__name__)

AIRKOREA_CTPRVN_RLTM_URL = (
    "https://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty"
)
# ver 1.4 는 1.0~1.3 의 항목(PM2.5·24시간 예측이동농도·측정망·1시간 등급)을 모두 포함한다
OPERATION_VERSION = "1.4"
# 시도 하나의 측정소는 수십 곳이다. 한 페이지로 받는다
NUM_OF_ROWS = 100
# 도시별 결과 재사용 시간 (#65). 에어코리아 측정값은 1시간 단위로 갱신된다
CACHE_SECONDS = 3600.0

# 포털 공통 응답 코드. 0 정상, 3 데이터없음, 그 외는 호출 실패
RESULT_OK = 0
RESULT_NODATA = 3

# 도시 대표값은 도시대기 측정망으로 낸다. 도로변대기는 통행량 때문에 높게 치우친다
MANG_URBAN = "도시대기"
# 농도·등급이 없을 때 에어코리아가 쓰는 표기
MISSING_VALUES = frozenset({"-", ""})


class AirGrade(StrEnum):
    """미세먼지 등급. 에어코리아 등급 코드 1~4 에 대응한다."""

    GOOD = "good"
    MODERATE = "moderate"
    BAD = "bad"
    VERY_BAD = "very_bad"


# 등급 코드 → 등급. 숫자가 클수록 나쁘다
GRADE_BY_CODE = {
    "1": AirGrade.GOOD,
    "2": AirGrade.MODERATE,
    "3": AirGrade.BAD,
    "4": AirGrade.VERY_BAD,
}
GRADE_ORDER = (AirGrade.GOOD, AirGrade.MODERATE, AirGrade.BAD, AirGrade.VERY_BAD)

# 도시 코드 → 에어코리아 시도명. 도시 이름을 로직에 박지 않고 이 표 하나로만 바꾼다
# (기술문서 요청 명세의 sidoName 코드값표)
SIDO_BY_CITY = {
    "seoul": "서울",
    "busan": "부산",
    "daegu": "대구",
    "incheon": "인천",
    "daejeon": "대전",
    "gwangju": "광주",
    "ulsan": "울산",
    "sejong": "세종",
    "gyeonggi": "경기",
    "gangwon": "강원",
    "chungbuk": "충북",
    "chungnam": "충남",
    "jeonbuk": "전북",
    "jeonnam": "전남",
    "gyeongbuk": "경북",
    "gyeongnam": "경남",
    "jeju": "제주",
}

PM10_VALUE = "pm10Value"
PM25_VALUE = "pm25Value"
PM10_GRADE = "pm10Grade"
PM25_GRADE = "pm25Grade"


class AirQuality(BaseModel):
    """service 가 쓰는 미세먼지. 값이 없으면 None 이고, 그래도 카드는 만든다."""

    model_config = ConfigDict(frozen=True)

    grade: AirGrade | None = Field(None, description="미세먼지 등급 (측정소 중 가장 나쁜 등급)")
    pm10: int | None = Field(None, description="PM10 농도 평균 (㎍/㎥)")
    pm25: int | None = Field(None, description="PM2.5 농도 평균 (㎍/㎥)")


class AirKoreaClient:
    def __init__(
        self,
        service_key: str,
        *,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        """
        :param service_key: 공공데이터포털 인증키 (Decoding 키). 쿼리로 보낼 때 인코딩된다
        :param transport: 테스트가 가짜 서버를 끼우는 자리. 운영에서는 None (실제 네트워크)
        :param cache_seconds: 같은 도시 결과를 재사용하는 시간 (#65). 측정값은 1시간 단위로 바뀐다
        """
        self._service_key = service_key
        self._timeout = timeout
        self._transport = transport
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, AirQuality]] = {}

    async def pm(self, city: str) -> AirQuality:
        """
        도시의 미세먼지 등급·농도를 받는다.

        :param city: 도시 코드 (User.city). SIDO_BY_CITY 로 시도명을 찾는다
        :return: 등급·PM10·PM2.5. 값이 없으면 각 필드가 None
        :raises AppException: EXTERNAL_API_FAILED - 키 없음, 네트워크 오류, 4xx/5xx,
            파싱 실패, 결측(03)이 아닌 에러 코드
        :raises ValueError: 표에 없는 도시 코드 (설정이나 호출하는 쪽의 버그)
        """
        # 1. 같은 도시는 값이 같다. 1시간 안이면 지난 결과를 쓴다 (미리 만들기가 사용자마다 부름)
        cached = self._cache.get(city)
        if cached is not None and time.monotonic() - cached[0] < self._cache_seconds:
            return cached[1]

        # 2. 호출 전 검사. 키가 없으면 네트워크를 타지 않고 바로 실패한다
        if not self._service_key:
            logger.error("AIRKOREA_SERVICE_KEY 가 비어 있다")
            raise AppException(ErrorCode.EXTERNAL_API_FAILED)
        sido_name = SIDO_BY_CITY.get(city)
        if sido_name is None:
            raise ValueError(f"에어코리아 시도명을 모르는 도시 코드다: {city}")

        # 3. 호출. httpx 예외는 여기서 끝낸다
        raw = await self._get(
            {
                "serviceKey": self._service_key,
                "returnType": "json",
                "numOfRows": str(NUM_OF_ROWS),
                "pageNo": "1",
                "sidoName": sido_name,
                "ver": OPERATION_VERSION,
            }
        )

        # 4. 파싱하고 기억한다
        air = parse_pm_response(raw)
        self._cache[city] = (time.monotonic(), air)
        return air

    async def _get(self, params: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(AIRKOREA_CTPRVN_RLTM_URL, params=params)
                if response.is_error:
                    # 인증키는 로그에 남기지 않는다
                    logger.error("에어코리아 시도별 실시간 → %s", response.status_code)
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
        except (httpx.HTTPError, ValueError) as exc:
            raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc


def parse_pm_response(raw: dict[str, Any]) -> AirQuality:
    """
    시도별 실시간 측정정보에서 도시 대표값을 만든다.

    농도는 측정소 평균(반올림), 등급은 측정소 중 가장 나쁜 등급이다. 도시 한쪽이 '나쁨'인데
    카드가 '좋음'이라고 말하지 않게 등급은 보수적으로 고른다.

    :param raw: getCtprvnRltmMesureDnsty JSON 응답
    :return: 등급·PM10·PM2.5. 결측이면 각 필드가 None
    :raises AppException: EXTERNAL_API_FAILED - 기대한 필드가 없거나 결측 아닌 에러 코드
    """
    # 1. 응답 코드 확인. 결측(03)은 빈 AirQuality, 그 외 에러는 호출 실패다
    code = _result_code(raw)
    if code == RESULT_NODATA:
        logger.info("에어코리아 결측 (NODATA)")
        return AirQuality()
    if code != RESULT_OK:
        logger.error("에어코리아 에러 코드 %s", code)
        raise AppException(ErrorCode.EXTERNAL_API_FAILED)

    try:
        items = raw["response"]["body"]["items"]
        # 측정소가 하나면 리스트가 아닐 수 있다
        if isinstance(items, dict):
            items = [items]
        stations: list[dict[str, Any]] = list(items)
    except (KeyError, TypeError) as exc:
        logger.error("에어코리아 응답 파싱 실패: %s", exc)
        raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc

    # 2. 도시대기 측정망만 쓴다. 한 곳도 없으면 받은 측정소를 그대로 쓴다
    urban = [station for station in stations if station.get("mangName") == MANG_URBAN]
    used = urban or stations
    if not used:
        logger.info("에어코리아 결측 (빈 item)")
        return AirQuality()

    # 3. 농도는 값이 있는 측정소의 평균
    pm10 = _average(used, PM10_VALUE)
    pm25 = _average(used, PM25_VALUE)

    # 4. 등급은 값이 있는 측정소 중 가장 나쁜 것 (PM10·PM2.5 함께 본다)
    grade = _worst_grade(used)
    return AirQuality(grade=grade, pm10=pm10, pm25=pm25)


def _result_code(raw: dict[str, Any]) -> int | None:
    """응답 코드를 정수로 읽는다. 봉투가 기대와 다르면 None (호출 실패로 다룬다)."""
    try:
        code = str(raw["response"]["header"]["resultCode"])
    except (KeyError, TypeError):
        return None
    return int(code) if code.isdigit() else None


def _read_int(station: dict[str, Any], key: str) -> int | None:
    """측정값을 정수로 읽는다. 결측('-', null, 빈 문자열)이면 None."""
    raw_value = station.get(key)
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if text in MISSING_VALUES:
        return None
    try:
        return round(float(text))
    except ValueError:
        return None


def _average(stations: list[dict[str, Any]], key: str) -> int | None:
    values = [value for station in stations if (value := _read_int(station, key)) is not None]
    if not values:
        return None
    return round(sum(values) / len(values))


def _worst_grade(stations: list[dict[str, Any]]) -> AirGrade | None:
    grades = [
        grade
        for station in stations
        for key in (PM10_GRADE, PM25_GRADE)
        if (grade := GRADE_BY_CODE.get(str(station.get(key)))) is not None
    ]
    if not grades:
        return None
    return max(grades, key=GRADE_ORDER.index)


_airkorea_client: AirKoreaClient | None = None


def get_airkorea_client() -> AirKoreaClient:
    """도시별 캐시를 공유하려고 프로세스에 하나만 만든다."""
    global _airkorea_client
    if _airkorea_client is None:
        _airkorea_client = AirKoreaClient(get_settings().airkorea_service_key)
    return _airkorea_client
