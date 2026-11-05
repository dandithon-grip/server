# 시드 데이터

| 파일 | 내용 | 출처·상태 |
| --- | --- | --- |
| `places_daegu.csv` | 큐레이션 자연 지점 30곳 (`scripts/seed_places.py`) | 좌표는 2026-11-04 OpenStreetMap Nominatim 지오코딩 결과. 공원·호수·다리는 지형 자체의 좌표라 정확하고, 가로수길·유원지 입구(범어로, 동대구로, 들안길, 동촌유원지, 앞산 큰골)는 대표 지점이라 팀이 카카오맵에서 한 번 확인한다. 설명·계절 태그는 초안 |
| `demo_commute.csv` | 데모 동선 1행 (`scripts/seed_demo.py`). 2호선 수성구청→반월당, 퇴근 18:10 | 좌표는 범어동·동성로 근처 임의 지점. 팀이 바꿔도 된다. 정류장 이름은 `stations_daegu_line2.csv` 와 같아야 한다 |
| `stations_daegu_line2.csv` | 데모 2호선 정류장 반월당~만촌 6개 (`scripts/seed_stations.py`) | 2026-11-04 OSM Nominatim `railway/station` 노드 좌표. 만촌만 역 노드가 없어 2번 출구 정류장 좌표 |
| `city_bbox.csv` | 도시별 OSM 적재 범위 (`scripts/load_green_areas.py`, osmium extract) | 대구 행은 시내(군위군 제외)를 감싸는 사각형. 큐레이션 지점 30곳이 모두 들어가는지 테스트가 확인한다 |

녹지 폴리곤(`green_areas`)은 OSM 추출에서 받는다. 파일을 커밋하지 않으므로 절차는 [docs/data/osm.md](../docs/data/osm.md) 에 있다.

규칙
- 좌표를 추측해서 적지 않는다. 출처를 이 표에 남긴다
- `types` 는 `river|park|trees|sunset|bridge|view` 중 1개 이상, `|` 로 구분
- `pair_id` 가 있으면 같은 값이 정확히 2행 (하천 진입·진출 쌍)
