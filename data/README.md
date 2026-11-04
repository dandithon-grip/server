# 시드 데이터

| 파일 | 내용 | 출처·상태 |
| --- | --- | --- |
| `places_daegu.csv` | 큐레이션 자연 지점 30곳 (`scripts/seed_places.py`) | 좌표는 2026-11-04 OpenStreetMap Nominatim 지오코딩 결과. 공원·호수·다리는 지형 자체의 좌표라 정확하고, 가로수길·유원지 입구(범어로, 동대구로, 들안길, 동촌유원지, 앞산 큰골)는 대표 지점이라 팀이 카카오맵에서 한 번 확인한다. 설명·계절 태그는 초안 |
| `stations_daegu_line2.csv` | 데모 2호선 정류장 (`scripts/seed_stations.py`) | 좌표 비어 있음. 팀이 채운다 |

규칙
- 좌표를 추측해서 적지 않는다. 출처를 이 표에 남긴다
- `types` 는 `river|park|trees|sunset|bridge|view` 중 1개 이상, `|` 로 구분
- `pair_id` 가 있으면 같은 값이 정확히 2행 (하천 진입·진출 쌍)
