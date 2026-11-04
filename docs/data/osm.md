# OSM 녹지 적재 (#9)

F-04 녹지 % 는 `green_areas` 테이블을 읽는다. 이 문서는 OpenStreetMap 추출에서 그 테이블을
채우는 절차다. 로컬 docker compose 와 Supabase 모두 같은 절차이고, 다른 사람이 처음부터
그대로 따라 할 수 있도록 적는다. 받은 OSM 파일은 커밋하지 않는다 (`data/osm/` 은 .gitignore).

적재 범위(bbox)는 `data/city_bbox.csv` 한 곳에만 있다. osmium 추출과 적재 SQL 이 같은 값을
쓰고, 도시 이름은 명령줄 인자로만 들어간다.

## 0. 준비

```bash
brew install osmium-tool osm2pgsql
docker compose up -d          # 로컬 PostGIS
uv run alembic upgrade head   # green_areas 테이블 (#12 의 0004 마이그레이션)
```

## 1. 한국 전체 추출 받기 (2026-11-04 기준 277MB, 1회)

```bash
mkdir -p data/osm
curl -L -o data/osm/south-korea-latest.osm.pbf https://download.geofabrik.de/asia/south-korea-latest.osm.pbf
```

## 2. 도시 범위로 자르기

```bash
BBOX=$(awk -F, '$1=="daegu" {print $2","$3","$4","$5}' data/city_bbox.csv)
osmium extract --bbox "$BBOX" --set-bounds \
  -o data/osm/daegu.osm.pbf data/osm/south-korea-latest.osm.pbf
```

2026-11-04 기준 대구 조각은 7.2MB, 자르는 데 1초대였다 (osmium 1.19.1).

도시를 늘릴 때는 `data/city_bbox.csv` 에 행을 추가하고 `daegu` 자리만 바꾼다.

## 3. osm2pgsql 로 DB 에 넣기

```bash
PGPASSWORD=saetgil osm2pgsql --create \
  --host localhost --port 5432 --username saetgil --database saetgil \
  data/osm/daegu.osm.pbf
```

- 기본 출력(`pgsql`)이 `planet_osm_polygon` · `planet_osm_line` 을 만든다. 적재 SQL 이 이 두
  테이블만 읽는다
- 투영은 기본값(3857)으로 둬도 된다. 적재 SQL 이 `ST_Transform(way, 4326)` 으로 맞춘다
- Supabase 에 넣을 때는 접속 정보만 바꾼다 (`-d "postgresql://postgres:…@db.xxxx.supabase.co:5432/postgres"`).
  PostGIS 확장이 먼저 켜져 있어야 한다

## 4. green_areas 채우기

```bash
uv run python -m scripts.load_green_areas daegu
# daegu 녹지 1959개 (forest 534, grass 165, park 871, river 349, trees 40) / 범위 128.35,35.6,128.8,36.05
```

위 숫자는 2026-11-04 추출의 실측값이다 (osm2pgsql 2.3.1 적재 2초, 적재 SQL 3초).

- SQL 은 `scripts/sql/load_green_areas.sql` 하나다. 태그 → `kind` 대응은 이렇다

| OSM 태그 | 테이블 | kind | 도형 |
| --- | --- | --- | --- |
| `leisure=park` | polygon | `park` | MULTIPOLYGON |
| `landuse=forest`, `natural=wood` | polygon | `forest` | MULTIPOLYGON |
| `landuse=grass` | polygon | `grass` | MULTIPOLYGON |
| `natural=water` (1ha 이상) | polygon | `river` | MULTIPOLYGON |
| `waterway=river` | line | `river` | MULTILINESTRING |
| `natural=tree_row` | line | `trees` | MULTILINESTRING |

- `natural=water` 는 **1ha 이상만** 물가로 친다 (`MIN_WATER_AREA_M2`). 공원 안 연못까지 넣으면
  도심 공원을 지나는 경로가 하천 길로 잡힌다 (실측: 경상감영공원 374m2 연못이 물가 가산 +5 를 받았다)
- `(osm_id, kind)` 로 upsert 한다. 여러 번 돌려도 결과가 같고, 손으로 넣은 행(`osm_id` 없음)은
  지우지 않는다
- 적재 조건(태그·면적 하한)을 바꾼 뒤에는 upsert 가 옛 행을 지우지 않으므로 그 도시만 비우고 다시 넣는다

```bash
docker compose exec -T db psql -U saetgil -d saetgil \
  -c "DELETE FROM green_areas WHERE city = 'daegu' AND osm_id IS NOT NULL;"
```
- 적재가 끝나면 `planet_osm_*` 테이블은 지워도 된다 (`DROP TABLE planet_osm_point,
  planet_osm_line, planet_osm_polygon, planet_osm_roads;`). 다시 적재할 거면 둔다

## 5. 확인 (인수 조건)

```bash
docker compose exec -T db psql -U saetgil -d saetgil -c \
  "SELECT kind, count(*) FROM green_areas WHERE city = 'daegu' GROUP BY kind ORDER BY kind;"
```

신천 하천 선과 경상감영공원 면이 들어왔는지는 테스트로 본다 (좌표는 `data/places_daegu.csv` 기준).

```bash
uv run pytest -m integration
```

`uv run pytest` 는 이 테스트를 건너뛴다. OSM 적재가 끝난 DB 에서만 통과한다.
다른 테스트는 `_test` DB 를 쓰지만 이 테스트만 `DATABASE_URL` 의 DB 를 읽는다 (적재를 거기에 하므로).
CI 는 OSM 데이터가 없으니 돌지 않는다.

## 메모

- 녹지 원본은 OpenStreetMap 이다. 배포본은 Geofabrik 의 south-korea-latest.osm.pbf.
  라이선스가 ODbL 이라 지도·데이터를 보여주는 화면에 "© OpenStreetMap contributors" 표기가 필요하다
- 대구 도시공원 CSV(공공데이터포털)는 폴리곤 보강용 후보로 남겨뒀다. 아직 내려받아 확인하지
  않았다 (좌표만 있는지 경계 폴리곤이 있는지 미확인). 공원 경계가 부족하면 그때 본다
- Overpass API 실시간 조회는 쓰지 않는다 (호출 한도·지연)
