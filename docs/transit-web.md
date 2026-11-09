# 대중교통 검색 (ODsay, 프론트 직접 호출, #73)

## 왜 프론트가 검색하나

ODsay 키는 종류에 따라 누가 부르는지를 다르게 확인한다.

| 플랫폼 | ODsay 가 확인하는 것 | 우리 |
| --- | --- | --- |
| Server | 요청이 온 IP | Railway 의 나가는 IP 가 고정이 아니라 등록할 수 없다. 운영 `GET /transit/routes` 는 502 |
| URI (웹) | 요청한 웹페이지의 도메인 | 사용자 브라우저가 우리 사이트에서 부르면 통과 |

그래서 운영에서는 브라우저가 ODsay 를 부르고, 서버는 받은 응답을 해석만 한다.
`GET /transit/routes` 는 로컬 개발용으로 남는다 (Mac IP 가 Server 플랫폼에 등록돼 있음).

## 흐름

```
1. 프론트 → ODsay searchPubTransPathT (URI 플랫폼 키)
2. 프론트 → POST /transit/routes/parse  { "odsay": 1의 응답 그대로 }
            ← GET /transit/routes 와 같은 응답 (summary, legs, stops[].external_id)
3. 사용자가 경로 하나 선택
4. 프론트 → PUT /commutes/me  transit_legs = 고른 경로의 legs 중 kind != "walk" (지금과 동일)
```

## ODsay 설정 (승래)

ODsay LAB › Application › saetgil › 설정 › 서비스 플랫폼 환경에서 **URI** 를 켜고
`http://localhost:3000` 과 프론트 배포 주소(`https://xxx.vercel.app`)를 등록한다. 반영까지 최대 1분.

## 프론트 코드 예시

```ts
const odsay = await fetch(
  `https://api.odsay.com/v1/api/searchPubTransPathT?SX=${fromLng}&SY=${fromLat}&EX=${toLng}&EY=${toLat}` +
    `&apiKey=${encodeURIComponent(ODSAY_KEY)}`
).then((r) => r.json());

const res = await fetch(`${API}/transit/routes/parse`, {
  method: "POST",
  headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
  body: JSON.stringify({ odsay }),
});
// 200: { paths: [...] }  /  422: 검색 결과 없음·형식 오류 ({"message": ...})
```

좌표 순서에 주의한다. ODsay 는 `SX`/`EX` 가 경도, `SY`/`EY` 가 위도다.

## 경로 그리기: journey (#77)

`GET /commutes/me/routes`·`/cards/today` 의 경로마다 `journey` 가 있다. 그 경로를 골랐을 때의
출발 → 도착 전체 여정을 조각 순서로 준다. `polyline` 은 그중 걷는 조각 하나일 뿐이므로
지도에는 **journey 를 그린다.** 후보 하나와 최단 하나를 각각 journey 로 그리면 선이 두 개 겹치지 않고
"같은 여정에서 어디가 바뀌었는지" 가 보인다.

| kind | role | 그리기 |
| --- | --- | --- |
| walk | usual | 회색 실선 |
| walk | saetgil | 초록 굵은 실선 (오늘 바뀐 구간) |
| walk | after_arrival | 초록 점선. 카드 문구는 "도착하고 N분 산책" |
| ride | usual | 노선색 또는 회색 점선 + 양 끝 정류장 마커 (`from_name`, `to_name`, `line`) |
| drive | usual | 회색 점선 |

```
퇴근(direction=to_home) 대중교통, 뒤 도보 후보를 골랐을 때
walk usual   회사 → 반월당역
ride usual   반월당 → 경대병원 → 대구은행 → 범어 → 수성구청 (대구 2호선)
walk saetgil 수성구청역 → 들안길 → 집
```

- 조각은 이어진다: `journey[i].polyline[-1] == journey[i+1].polyline[0]`
- 탑승 조각은 정류장 좌표를 잇는 선이라 실제 노선 모양과 다르다. 점선이 자연스럽다
- 정류장 행이 없는 경우(삭제 등) 탑승이 두 점(탑승·하차)만으로 올 수 있다
- 방향: `to_home` 은 회사에서 시작한다. 화면의 출발지 표시도 그에 맞춘다
