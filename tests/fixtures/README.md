# 외부 API 응답 픽스처

테스트는 네트워크를 타지 않는다. 실제 응답을 여기 두고 MockTransport 로 돌려준다.

| 파일 | 출처 | 비고 |
| --- | --- | --- |
| `tmap_pedestrian.json` | TMAP 보행자 경로 API 실제 호출 (2026-11-04). 범어동(35.8581, 128.6305) → 동성로(35.8651, 128.5935), 경유지 1곳 | 키 값은 응답에 포함되지 않는다. 공식 문서 샘플에는 `totalTime`·`totalDistance` 가 빠져 있어 실제 응답을 쓴다 |
| `kma_vilage_fcst.json` | 기상청 단기예보조회(getVilageFcst) 실제 호출 (2026-11-04). 대구 격자 nx=89, ny=90 · base 20261104 1700 · `dataType=JSON` · numOfRows=300 | 문서 샘플은 XML 1건뿐이라 JSON 봉투(`response.body.items.item`)와 SKY·PTY·TMP 조합을 실제 응답으로 둔다. 2026-11-05 17시 슬롯에 PTY=1(비) 이 들어 있다 |
| `airkorea_ctprvn_rltm.json` | 에어코리아 시도별 실시간 측정정보(getCtprvnRltmMesureDnsty) 실제 호출 (2026-11-04). `sidoName=대구` · `returnType=json` · `ver=1.4` | 측정소 26곳. 수창동은 값이 `'-'` 이고 등급이 `null`, 본동은 값만 있고 등급이 `null` 이라 결측 경로를 실제 응답으로 덮는다 |
