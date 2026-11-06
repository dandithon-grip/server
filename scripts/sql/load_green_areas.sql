-- osm2pgsql 이 적재한 OSM 테이블에서 녹지를 골라 green_areas 로 넣는다.
-- 실행은 scripts/load_green_areas.py, 절차는 docs/data/osm.md.
-- 바인드: :city :min_lng :min_lat :max_lng :max_lat :min_water_area_m2
-- (도시 이름과 조건값을 SQL 에 쓰지 않는다)
--
-- 공원·숲·잔디·물은 면(MULTIPOLYGON), 하천·가로수열은 OSM 에서 선(MULTILINESTRING)이다.
-- green_areas 의 CHECK 가 둘 다 받으므로 ST_CollectionExtract 로 종류를 맞춰 넣는다.
-- (osm_id, kind) 가 겹치면 geom 을 갱신한다. 여러 번 실행해도 결과가 같다.
WITH bounds AS (
    SELECT ST_MakeEnvelope(
        CAST(:min_lng AS double precision),
        CAST(:min_lat AS double precision),
        CAST(:max_lng AS double precision),
        CAST(:max_lat AS double precision),
        4326
    ) AS geom
),
areas AS (
    SELECT
        p.osm_id,
        NULLIF(p.name, '') AS name,
        CASE
            WHEN p.leisure = 'park' THEN 'park'
            WHEN p.landuse = 'forest' OR p."natural" = 'wood' THEN 'forest'
            WHEN p.landuse = 'grass' THEN 'grass'
            WHEN p."natural" = 'water' THEN 'river'
        END AS kind,
        -- 3 = 폴리곤만 추려 MULTIPOLYGON 으로. osm2pgsql 이 3857 로 넣었어도 4326 으로 맞춘다
        ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Transform(p.way, 4326)), 3)) AS geom
    FROM planet_osm_polygon AS p, bounds AS b
    WHERE ST_Intersects(ST_Transform(p.way, 4326), b.geom)
      AND (
          p.leisure = 'park'
          OR p.landuse IN ('forest', 'grass')
          OR p."natural" = 'wood'
          OR (
              -- 호수·저수지는 물가로 치되 공원 안 연못은 뺀다. 작은 물웅덩이까지 river 로 넣으면
              -- 도심 공원을 지나는 경로가 하천 길로 잡힌다 (실측: 경상감영공원 374m2 연못)
              p."natural" = 'water'
              AND ST_Area(CAST(ST_Transform(p.way, 4326) AS geography))
                  >= CAST(:min_water_area_m2 AS double precision)
          )
      )
),
lines AS (
    SELECT
        l.osm_id,
        NULLIF(l.name, '') AS name,
        CASE
            WHEN l.waterway = 'river' THEN 'river'
            WHEN l."natural" = 'tree_row' THEN 'trees'
        END AS kind,
        -- 2 = 선만 추려 MULTILINESTRING 으로
        ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Transform(l.way, 4326)), 2)) AS geom
    FROM planet_osm_line AS l, bounds AS b
    WHERE ST_Intersects(ST_Transform(l.way, 4326), b.geom)
      AND (l.waterway = 'river' OR l."natural" = 'tree_row')
),
picked AS (
    -- 한 way 에 녹지 태그가 여럿이면 kind 가 같은 행이 두 번 나올 수 있다.
    -- 같은 (osm_id, kind) 를 한 INSERT 안에서 두 번 갱신하면 Postgres 가 거부하므로 하나만 둔다
    SELECT DISTINCT ON (osm_id, kind) osm_id, name, kind, geom
    FROM (
        SELECT osm_id, name, kind, geom FROM areas
        UNION ALL
        SELECT osm_id, name, kind, geom FROM lines
    ) AS candidates
    WHERE kind IS NOT NULL
      AND NOT ST_IsEmpty(geom)
    ORDER BY osm_id, kind
)
INSERT INTO green_areas (id, kind, name, geom, osm_id, city)
SELECT gen_random_uuid(), kind, name, geom, osm_id, CAST(:city AS varchar(20))
FROM picked
ON CONFLICT (osm_id, kind) DO UPDATE
SET geom = EXCLUDED.geom,
    name = EXCLUDED.name,
    city = EXCLUDED.city
