"""프로젝트의 로컬 시간대.

퇴근 시각(F-03 노을 창)과 기록 날짜(F-11 "오늘")가 같은 기준을 쓴다.
기상청(app/clients/kma.py)의 KST 는 발표시각 규격이라 별개로 둔다 - 지역이 바뀌어도 기상청은 KST 다.
"""

from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Seoul")
