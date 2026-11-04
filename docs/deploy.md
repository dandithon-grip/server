# 배포 · CI (#17)

심사용 외부 URL 은 Railway 로, PR 검사는 GitHub Actions 로 한다.

## CI

`.github/workflows/ci.yml` 이 PR 과 develop 푸시에서 돈다.

| 단계 | 명령 |
| --- | --- |
| 린트 | `uv run ruff check .` |
| 포맷 | `uv run ruff format --check .` |
| 타입 | `uv run mypy app scripts` |
| 테스트 | `uv run pytest` |

DB 는 서비스 컨테이너로 `postgis/postgis:17-3.5` 를 띄운다. 로컬 docker-compose 와 같은 이미지다.
서비스 컨테이너에는 `docker/initdb/` 를 걸 수 없어서 테스트 DB(`saetgil_test`)를 워크플로 단계에서 만든다.
postgis 확장은 alembic `0001` 이 `CREATE EXTENSION IF NOT EXISTS postgis` 로 넣으므로 따로 설치하지 않는다.

CI 에 들어가는 환경변수는 전부 더미다. JWKS 조회는 `tests/conftest.py` 가 테스트용 키 쌍으로 바꿔치기해서
실제 Supabase 프로젝트가 없어도 돈다. **키 값은 워크플로에 쓰지 않는다.**

브랜치 보호 규칙은 걸지 않는다 (11/4 결정). CI 결과는 PR 화면에 표시되고, 병합을 막지는 않는다.

## Railway

main 푸시 → 자동 배포. develop 은 배포하지 않는다.

### 처음 한 번 (대시보드)

1. Railway 에서 New Project → Deploy from GitHub repo → `dandithon-grip/server`
2. Settings → Source 의 배포 브랜치를 `main` 으로 바꾼다 (기본값이 default branch 인 develop 이다)
3. Variables 에 아래를 넣는다

   | 키 | 값 |
   | --- | --- |
   | `DATABASE_URL` | Supabase 의 **Session pooler** 문자열. 앞을 `postgresql+asyncpg://` 로 바꾼다 |
   | `SUPABASE_URL` | `https://{프로젝트}.supabase.co` |
   | `SUPABASE_JWT_AUDIENCE` | `authenticated` |
   | `DEFAULT_CITY` | `daegu` |
   | `APP_ENV` | `prod` |
   | `TMAP_APP_KEY` 외 외부 API 키 | 해당 이슈가 병합된 뒤에 넣는다 |

   `SUPABASE_SERVICE_ROLE_KEY` · `DEMO_EMAIL` · `DEMO_PASSWORD` 는 **넣지 않는다.**
   `scripts/seed_demo.py` 전용이고 앱 런타임은 읽지 않는다. 서버 프로세스에 마스터 키를 두지 않기 위해서다.

4. Networking → Generate Domain 으로 외부 URL 을 받는다

### 시작 명령

`railway.json` 의 `deploy.startCommand` 에 있다.

```
sh -c 'uv run alembic upgrade head && uv run uvicorn app.main:app --host 0.0.0.0 --port $PORT'
```

마이그레이션이 서버 기동 앞에 붙어 있어서, main 에 머지하면 Supabase 스키마가 자동으로 따라온다.
`$PORT` 는 Railway 가 주입하므로 `sh -c` 로 감싸 셸이 치환하게 한다.

> 첫 배포에서 시작 명령이 그대로 먹는지 로그로 확인한다. Railway 가 대시보드의 Custom Start Command 를
> 우선할 수 있어서, 안 먹으면 위 문자열을 Settings → Deploy → Custom Start Command 에 그대로 넣는다.

### Supabase 쪽 확인

배포 전에 Supabase SQL Editor 에서 한 번 확인한다.

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
SELECT postgis_version();
```

`DATABASE_URL` 은 Direct connection 이 아니라 **Session pooler** 를 쓴다. Railway 는 IPv4 에서 나가고
Supabase 의 Direct connection 은 IPv6 라 연결이 안 된다.

## 확인

배포 뒤 이 셋을 본다.

```
GET {외부 URL}/docs          → 200, Swagger 화면
GET {외부 URL}/users/me      → 401 (토큰 없음)
GET {외부 URL}/users/me      → 200 (Supabase access token 으로)
```
