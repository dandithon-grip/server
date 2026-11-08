"""기상청 단기예보 조회서비스 래퍼.

F-05 가 카드 문장과 홈 헤더에 쓸 하늘상태·기온을 받는다. 노을 가산(F-04)은 하늘상태가
맑음일 때만 주므로 sky 값이 그 판단의 입력이다. service 는 httpx 를 직접 부르지 않고
이 클라이언트의 Weather 만 본다.

엔드포인트: GET https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst
- 쿼리 serviceKey(공공데이터포털 인증키)·dataType=JSON·base_date·base_time·nx·ny
- 응답: response.header.resultCode 와 response.body.items.item[] (한 행 = 한 예보시각의 한 항목)
- 발표는 1일 8회(02·05·08·11·14·17·20·23시)이고 발표 10분 뒤부터 제공된다
- 하늘상태(SKY) 1 맑음 / 3 구름많음 / 4 흐림, 강수형태(PTY, 단기) 0 없음 / 1 비 / 2 비·눈 /
  3 눈 / 4 소나기, 기온(TMP) ℃
근거: 공공데이터포털 활용가이드 '단기예보조회서비스_API활용가이드_261019' (응답 명세·코드값·
      에러코드·격자 변환 소스), 격자 좌표 엑셀 '기상청41_...격자_위경도(2610).xlsx'

값 결측(발표 시각 밖의 NODATA, 해당 슬롯에 항목 없음)은 예외가 아니라 None 필드로 돌려준다.
날씨를 못 받았다고 카드 생성 전체가 막히면 안 된다 (#6 결정).
"""

import logging
import math
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.core.exceptions import AppException, ErrorCode

logger = logging.getLogger(__name__)

KMA_VILAGE_FCST_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
KST = ZoneInfo("Asia/Seoul")

# 단기예보 발표시각(KST)과 제공 시작 지연. 활용가이드 '예보 발표시각'
BASE_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)
PUBLISH_DELAY = timedelta(minutes=10)
# 한 발표에 12개 항목이 시각별로 들어온다. 300행이면 발표 시점부터 약 하루치를 덮는다
NUM_OF_ROWS = 300

# 포털 공통 응답 코드. 0 정상, 3 데이터없음, 그 외는 호출 실패
RESULT_OK = 0
RESULT_NODATA = 3

CATEGORY_SKY = "SKY"
CATEGORY_PTY = "PTY"
CATEGORY_TEMPERATURE = "TMP"


class WeatherSky(StrEnum):
    """카드가 쓰는 하늘상태. 기상청 SKY·PTY 코드를 네 가지로 좁힌 값이다."""

    CLEAR = "clear"
    CLOUDY = "cloudy"
    RAIN = "rain"
    SNOW = "snow"


# SKY 코드 → 하늘상태. 2 는 기상청이 쓰지 않는다
SKY_BY_CODE = {"1": WeatherSky.CLEAR, "3": WeatherSky.CLOUDY, "4": WeatherSky.CLOUDY}
# PTY 코드 → 하늘상태. 강수가 있으면 하늘상태를 덮는다.
# '비/눈'(2)은 눈보다 비로 본다. 길이 젖는 쪽이 걷기 판단에 가깝다
PRECIPITATION_BY_CODE = {
    "1": WeatherSky.RAIN,
    "2": WeatherSky.RAIN,
    "3": WeatherSky.SNOW,
    "4": WeatherSky.RAIN,
}
PTY_NONE = "0"

# 단기예보 Lambert Conformal Conic 지도 정보. 활용가이드 부록의 변환 소스 그대로다
GRID_EARTH_RADIUS_KM = 6371.00877
GRID_SPACING_KM = 5.0
GRID_STANDARD_LAT1 = 30.0
GRID_STANDARD_LAT2 = 60.0
GRID_ORIGIN_LNG = 126.0
GRID_ORIGIN_LAT = 38.0
GRID_ORIGIN_X = 210.0 / GRID_SPACING_KM
GRID_ORIGIN_Y = 675.0 / GRID_SPACING_KM


class Grid(BaseModel):
    """단기예보 격자 좌표. 위경도 1곳이 5km 격자 한 칸에 대응한다."""

    model_config = ConfigDict(frozen=True)

    nx: int = Field(..., description="예보지점 X 좌표")
    ny: int = Field(..., description="예보지점 Y 좌표")


class Weather(BaseModel):
    """service 가 쓰는 날씨. 값이 없으면 None 이고, 그래도 카드는 만든다."""

    model_config = ConfigDict(frozen=True)

    sky: WeatherSky | None = Field(None, description="하늘상태. 노을 가산은 clear 일 때만")
    temp_c: float | None = Field(None, description="기온 (℃)")


def to_grid(lat: float, lng: float) -> Grid:
    """
    위경도를 단기예보 격자(nx, ny)로 바꾼다.

    활용가이드 부록의 C 소스(lamcproj + map_conv, code 0)를 그대로 옮긴 것이다.
    격자 좌표 엑셀의 전 행(3,801개) 중 98.9% 가 일치하고, 어긋나는 행은 행정구역
    대표점이 격자 경계에 걸린 경우다.

    :param lat: 위도
    :param lng: 경도
    :return: 격자 좌표
    """
    # 1. 지도 상수 (라디안·격자 단위)
    degrad = math.pi / 180.0
    re = GRID_EARTH_RADIUS_KM / GRID_SPACING_KM
    slat1 = GRID_STANDARD_LAT1 * degrad
    slat2 = GRID_STANDARD_LAT2 * degrad
    olon = GRID_ORIGIN_LNG * degrad
    olat = GRID_ORIGIN_LAT * degrad

    # 2. 두 표준위도로 원뿔 계수(sn)와 축척(sf, ro)을 구한다
    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
    sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5)
    ro = re * sf / math.pow(ro, sn)

    # 3. 투영 후 기준점 격자를 더한다. 소스의 (int)(x + 1.5) 가 1 기반 격자 번호다
    ra = math.tan(math.pi * 0.25 + lat * degrad * 0.5)
    ra = re * sf / math.pow(ra, sn)
    theta = lng * degrad - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn

    x = ra * math.sin(theta) + GRID_ORIGIN_X
    y = ro - ra * math.cos(theta) + GRID_ORIGIN_Y
    return Grid(nx=int(x + 1.5), ny=int(y + 1.5))


def _as_kst(value: datetime) -> datetime:
    """tzinfo 가 없으면 KST 로 보고, 있으면 KST 로 바꿔 비교할 수 있게 한다."""
    return value.astimezone(KST) if value.tzinfo else value.replace(tzinfo=KST)


def latest_base(at: datetime) -> tuple[str, str]:
    """
    기준 시각에 이미 제공되고 있는 가장 최근 발표(base_date, base_time)를 고른다.

    발표 10분 뒤부터 제공되므로 17:05 에는 아직 14시 발표를 쓴다.

    :param at: 예보를 알고 싶은 시각. tzinfo 가 없으면 KST 로 본다
    :return: ("20261104", "1700") 형식
    """
    at_kst = at.astimezone(KST).replace(tzinfo=None) if at.tzinfo else at

    # 1. 같은 날 발표 중 이미 제공 중인 가장 늦은 것
    for hour in reversed(BASE_HOURS):
        base = at_kst.replace(hour=hour, minute=0, second=0, microsecond=0)
        if base + PUBLISH_DELAY <= at_kst:
            return base.strftime("%Y%m%d"), base.strftime("%H%M")

    # 2. 자정 직후라면 전날 23시 발표가 가장 최근이다
    base = (at_kst - timedelta(days=1)).replace(
        hour=BASE_HOURS[-1], minute=0, second=0, microsecond=0
    )
    return base.strftime("%Y%m%d"), base.strftime("%H%M")


class KmaClient:
    def __init__(
        self,
        service_key: str,
        *,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        :param service_key: 공공데이터포털 인증키 (Decoding 키). 쿼리로 보낼 때 인코딩된다
        :param transport: 테스트가 가짜 서버를 끼우는 자리. 운영에서는 None (실제 네트워크)
        """
        self._service_key = service_key
        self._timeout = timeout
        self._transport = transport

    async def forecast(
        self, lat: float, lng: float, at: datetime, *, now: datetime | None = None
    ) -> Weather:
        """
        한 지점의 특정 시각 날씨를 받는다.

        :param lat: 위도
        :param lng: 경도
        :param at: 예보를 알고 싶은 시각 (보통 퇴근 시각). tzinfo 가 없으면 KST
        :param now: 지금 시각. 생략하면 현재. 발표 시각은 지금 이미 나온 것 중에서 고른다 (#68)
        :return: 하늘상태·기온. 값이 없으면 각 필드가 None
        :raises AppException: EXTERNAL_API_FAILED - 키 없음, 네트워크 오류, 4xx/5xx,
            파싱 실패, 결측(03)이 아닌 에러 코드
        """
        # 1. 호출 전 검사. 키가 없으면 네트워크를 타지 않고 바로 실패한다
        if not self._service_key:
            logger.error("KMA_SERVICE_KEY 가 비어 있다")
            raise AppException(ErrorCode.EXTERNAL_API_FAILED)

        # 2. 격자 변환과 발표 시각 선택. 오전에 저녁 예보를 물으면 아직 없는 발표를 고르지 않게
        #    "지금"과 "예보 시각" 중 이른 쪽 기준으로 가장 최근 발표를 쓴다 (#68)
        grid = to_grid(lat, lng)
        current = now or datetime.now(KST)
        base_date, base_time = latest_base(min(_as_kst(at), _as_kst(current)))

        # 3. 호출. httpx 예외는 여기서 끝낸다
        raw = await self._get(
            {
                "serviceKey": self._service_key,
                "dataType": "JSON",
                "numOfRows": str(NUM_OF_ROWS),
                "pageNo": "1",
                "base_date": base_date,
                "base_time": base_time,
                "nx": str(grid.nx),
                "ny": str(grid.ny),
            }
        )

        # 4. 파싱
        return parse_forecast_response(raw, at)

    async def _get(self, params: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(KMA_VILAGE_FCST_URL, params=params)
                if response.is_error:
                    # 인증키는 로그에 남기지 않는다
                    logger.error("기상청 단기예보 → %s", response.status_code)
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
        except (httpx.HTTPError, ValueError) as exc:
            raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc


def parse_forecast_response(raw: dict[str, Any], at: datetime) -> Weather:
    """
    단기예보 응답에서 기준 시각에 해당하는 슬롯의 하늘상태·기온을 뽑는다.

    기준 시각 이하의 가장 늦은 예보시각을 쓰고, 그보다 이른 슬롯이 없으면 가장 이른 슬롯을 쓴다.

    :param raw: getVilageFcst JSON 응답
    :param at: 예보를 알고 싶은 시각
    :return: 하늘상태·기온. 결측이면 각 필드가 None
    :raises AppException: EXTERNAL_API_FAILED - 기대한 필드가 없거나 결측 아닌 에러 코드
    """
    # 1. 응답 코드 확인. 결측(03)은 빈 Weather, 그 외 에러는 호출 실패다
    code = _result_code(raw)
    if code == RESULT_NODATA:
        logger.info("기상청 단기예보 결측 (NODATA)")
        return Weather()
    if code != RESULT_OK:
        logger.error("기상청 단기예보 에러 코드 %s", code)
        raise AppException(ErrorCode.EXTERNAL_API_FAILED)

    try:
        body = raw["response"]["body"]
        items = body["items"]["item"]
        # 항목이 하나면 리스트가 아닐 수 있다
        if isinstance(items, dict):
            items = [items]

        # 2. 예보시각별로 항목을 모은다
        by_slot: dict[datetime, dict[str, str]] = {}
        for item in items:
            slot = datetime.strptime(f"{item['fcstDate']}{item['fcstTime']}", "%Y%m%d%H%M")
            by_slot.setdefault(slot, {})[item["category"]] = str(item["fcstValue"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("기상청 단기예보 응답 파싱 실패: %s", exc)
        raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc

    if not by_slot:
        logger.info("기상청 단기예보 결측 (빈 item)")
        return Weather()

    # 3. 기준 시각 이하의 가장 늦은 슬롯. 없으면 가장 이른 슬롯
    at_kst = at.astimezone(KST).replace(tzinfo=None) if at.tzinfo else at
    past_slots = [slot for slot in by_slot if slot <= at_kst]
    target = max(past_slots) if past_slots else min(by_slot)
    values = by_slot[target]

    # 4. 강수형태가 있으면 하늘상태를 덮는다
    return Weather(sky=_read_sky(values), temp_c=_read_temperature(values))


def _result_code(raw: dict[str, Any]) -> int | None:
    """응답 코드를 정수로 읽는다. 봉투가 기대와 다르면 None (호출 실패로 다룬다)."""
    try:
        code = str(raw["response"]["header"]["resultCode"])
    except (KeyError, TypeError):
        return None
    return int(code) if code.isdigit() else None


def _read_sky(values: dict[str, str]) -> WeatherSky | None:
    precipitation = values.get(CATEGORY_PTY)
    if precipitation is not None and precipitation != PTY_NONE:
        return PRECIPITATION_BY_CODE.get(precipitation)
    return SKY_BY_CODE.get(values.get(CATEGORY_SKY, ""))


def _read_temperature(values: dict[str, str]) -> float | None:
    raw_value = values.get(CATEGORY_TEMPERATURE)
    if raw_value is None:
        return None
    try:
        return float(raw_value)
    except ValueError:
        # 결측 표기(±900 이상, '-')는 값이 없는 것으로 본다
        return None


def get_kma_client() -> KmaClient:
    return KmaClient(get_settings().kma_service_key)
