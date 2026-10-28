# 샛길 Server

## 프로젝트 개요
샛길은 출퇴근 중 "걷는 구간"을 바꿔, 결심 없이 자연을 지나가게 하는 웹앱의 백엔드 서버다.
사용자가 출퇴근 동선을 한 번 등록하면 매일 퇴근 전 "오늘의 샛길" 카드 한 장을 받는다.
2026 단디톤 출품작. 팀 "기회는그립감이좋다".

핵심 기능:
- Supabase Auth(이메일·비밀번호) JWT 검증 + 사용자 upsert, 게스트(둘러보기) 접근. 가입·로그인 API 는 서버에 없다
- 출퇴근 설정 (출발·도착·시각·교통수단·정류장)
- 샛길 생성: TMAP 보행 경로 + PostGIS 녹지 점수 + 이벤트 점수
- 대체 로직 5단계 (샛길 없을 때)
- 오늘의 카드: 서버가 후보를 거르고 LLM이 선택·설명
- 기록 (사진 → 비전 계절 태그)

기획·명세 문서가 우선한다. 명세에 없는 기능은 만들지 않는다.

## 기술 스택
- Python 3.12 + FastAPI
- uv (의존성·가상환경), ruff (lint/format), mypy (strict)
- SQLAlchemy 2.0 (async) + asyncpg
- GeoAlchemy2 (PostGIS)
- Alembic (마이그레이션)
- Pydantic v2 + pydantic-settings
- PyJWT (Supabase JWT 검증, JWKS)
- httpx (TMAP · ODsay · 기상청 · LLM API 호출)
- pytest + pytest-asyncio + httpx AsyncClient
- Supabase (Postgres + PostGIS + Auth), Docker (로컬 Postgres/PostGIS)
- 배포: Railway 또는 Render, GitHub Actions (CI)

쓰지 않는 것: Redis, 메시지 큐, 벡터 DB, 파인튜닝, RAG, Django, Flask, 동기 SQLAlchemy.

## 패키지 구조 규칙
기능(도메인)별로 폴더를 나눈다. 도메인 하나는 파일 네 개다.

```
app/
  main.py                  앱 생성, 라우터 등록, 예외 핸들러 등록
  core/                    도메인에 종속되지 않는 공통 설정·보안·예외
    config.py              Settings (pydantic-settings)
    database.py            engine, session factory, get_db 의존성, Base, TimestampMixin
    security.py            JWT 검증, get_current_user / get_optional_user 의존성
    exceptions.py          AppException, ErrorCode, 전역 핸들러
  domain/
    auth/
      router.py            엔드포인트 + OpenAPI 명세 (summary, responses)
      service.py           비즈니스 로직 + DB 쿼리
      schemas.py           Request / Response 모델
      models.py            SQLAlchemy 엔티티
    user/
    commute/
    route/
    card/
    record/
  clients/                 외부 API 래퍼 (tmap.py, odsay.py, kma.py, llm.py)
alembic/
tests/
  conftest.py
  domain/{도메인}/test_*.py
```
- core/ : 스프링의 global/ 에 해당한다
- 도메인 간 참조는 service → 다른 도메인의 service 까지만. 다른 도메인의 models를 직접 쿼리하지 않는다
- repository 레이어는 두지 않는다. 쿼리는 service 에 쓰고 단계 주석으로 구분한다. 한 도메인의 쿼리가 200줄을 넘으면 그때 `queries.py` 로 분리한다
- clients/ : 외부 API는 service 에서 직접 httpx 를 부르지 않고 clients/ 래퍼를 통해 부른다
- 도시(대구/서울) 이름을 코드에 하드코딩하지 않는다. 모든 지리 데이터 조회는 `city` 파라미터를 받는다

## 코드 컨벤션

### schemas (DTO)
- Request : {동사}{명사}Request (예: CreateCommuteRequest)
- Response : {명사}Response (예: UserResponse, RouteResponse)
- 모든 모델은 Pydantic BaseModel. Request 는 `frozen=True`, Response 는 `from_attributes=True`
- 유효성 검증은 Field(...) 제약으로 schemas 에 둔다. service 에서 다시 검증하지 않는다
- Response 변환은 `UserResponse.model_validate(user)` 로 한다. from_model 같은 변환 메서드를 손으로 쓰지 않는다
- 엔티티를 router 밖으로 내보내지 않는다. 항상 Response 로 변환한다

```python
class CreateCommuteRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_lat: float = Field(..., ge=-90, le=90, description="출발 위도")
    from_lng: float = Field(..., ge=-180, le=180, description="출발 경도")
    leave_time: time = Field(..., description="퇴근 시각 (HH:MM)")
    transport_mode: TransportMode = Field(..., description="도보 | 대중교통 | 자동차")


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    nickname: str
    city: str
```

### router
- OpenAPI 명세(summary, description, responses)는 데코레이터에 바로 쓴다. FastAPI 에서는 데코레이터가 곧 명세다
- `APIRouter(prefix=..., tags=[...])` 에 prefix 와 tag 를 선언한다
- router 함수는 의존성 주입과 service 호출, Response 변환만 한다. 분기·계산·쿼리 금지
- 모든 router 함수에 `response_model` 과 반환 타입을 쓴다

```python
router = APIRouter(prefix="/users", tags=["User"])


@router.get(
    "/me",
    response_model=UserResponse,
    summary="내 정보 조회",
    description="Supabase JWT 로 인증된 사용자의 정보를 반환한다. 최초 호출 시 사용자를 생성한다.",
    responses={401: {"description": "토큰 없음 또는 만료"}},
)
async def me(current_user: User = Depends(get_current_user)) -> UserResponse:
    return UserResponse.model_validate(current_user)
```

### service
- 함수마다 docstring 으로 역할, 파라미터, 반환값, 예외를 명시한다
- 비즈니스 로직의 각 단계를 `# 1.` `# 2.` 인라인 주석으로 구분한다
- service 는 AsyncSession 을 생성자로 받는 클래스. router 에서 `Depends(get_auth_service)` 로 주입한다
- 트랜잭션 경계는 service 메서드 하나. commit 은 메서드 끝에서 한 번
- 쿼리는 service 안에 쓰되 `select()` 를 만드는 줄과 결과를 해석하는 줄을 분리해 읽기 쉽게 한다
- PostGIS 함수(ST_DWithin, ST_Subdivide 등)는 GeoAlchemy2 의 `func.ST_*` 로 쓴다. 문자열 SQL 은 쓰지 않는다

```python
class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_user(self, claims: SupabaseClaims) -> User:
        """
        Supabase JWT 클레임으로 사용자를 조회하거나 최초 로그인 시 생성한다.

        :param claims: 검증이 끝난 Supabase JWT 클레임 (sub = Supabase user id)
        :return: 조회 또는 생성된 User
        :raises AppException: INVALID_TOKEN - sub 클레임이 없음
        """
        # 1. sub 클레임 확인
        if not claims.sub:
            raise AppException(ErrorCode.INVALID_TOKEN)

        # 2. 기존 사용자 조회
        stmt = select(User).where(User.supabase_id == claims.sub)
        user = (await self._session.execute(stmt)).scalar_one_or_none()
        if user:
            return user

        # 3. 최초 로그인 - 사용자 생성 (닉네임은 user_metadata.nickname, 없으면 기본값)
        user = User(supabase_id=claims.sub, nickname=claims.nickname or "샛길러", city="daegu")
        self._session.add(user)
        await self._session.commit()
        await self._session.refresh(user)
        return user
```

### models (엔티티)
- SQLAlchemy 2.0 `Mapped[...]` + `mapped_column` 스타일. 레거시 `Column()` 금지
- `__tablename__` 명시. 테이블·컬럼은 snake_case
- 기본키는 UUID (`default=uuid4`). created_at, updated_at 은 core 의 TimestampMixin 으로
- 좌표는 `Geometry("POINT", srid=4326)`, 경로는 `Geometry("LINESTRING", srid=4326)`
- **집·회사 좌표는 저장하지 않는다.** Commute 에는 동 단위(area)와 정류장 id 만 저장한다. 좌표는 경로 생성 요청에서만 받고 버린다

```python
class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    supabase_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    nickname: Mapped[str] = mapped_column(String(50))
    city: Mapped[str] = mapped_column(String(20), default="daegu")
    notify_on: Mapped[bool] = mapped_column(default=True)
```

### 인증 (core/security.py)
- 가입·로그인은 Supabase Auth 가 한다 (프론트가 signUp / signInWithPassword 호출). 서버는 JWT 를 검증만 하고 회원가입·로그인 API 를 만들지 않는다
- 닉네임은 signUp 의 options.data.nickname → JWT 의 user_metadata.nickname 으로 들어온다. 없으면 "샛길러"
- 검증: Authorization: Bearer {token} → Supabase JWKS 로 서명 검증 → aud, exp 확인 → SupabaseClaims
- `get_current_user` : 토큰 필수. 없거나 만료면 AppException(ErrorCode.INVALID_TOKEN) → 401
- `get_optional_user` : 토큰 없으면 None. 둘러보기(게스트) 허용 엔드포인트에 쓴다
- 게스트는 별도 토큰을 발급하지 않는다. 게스트 허용 여부는 엔드포인트가 결정한다
- JWKS 는 앱 시작 시 한 번 받아 캐시하고, kid 불일치 시 한 번 재조회한다

### 예외 처리
- 비즈니스 예외는 `AppException(ErrorCode)` 하나만 쓴다. HTTPException 직접 사용 금지
- ErrorCode 는 Enum. HTTP 상태코드와 메시지를 함께 정의한다
- 모든 예외는 core/exceptions.py 의 전역 핸들러에서 처리한다. router 에서 try/except 로 응답을 만들지 않는다
- 에러 응답 형식: `{ "message": "에러 메시지" }`
- 외부 API 실패는 clients/ 에서 AppException(ErrorCode.EXTERNAL_API_FAILED) 로 바꿔 던진다. httpx 예외가 도메인까지 올라오지 않는다

```python
class ErrorCode(Enum):
    INVALID_TOKEN = (401, "유효하지 않은 토큰입니다")
    USER_NOT_FOUND = (404, "사용자를 찾을 수 없습니다")
    COMMUTE_NOT_FOUND = (404, "출퇴근 정보가 없습니다")
    EXTERNAL_API_FAILED = (502, "외부 API 호출에 실패했습니다")
    LLM_FAILED = (502, "카드 생성에 실패했습니다")

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message


# 사용 예시
raise AppException(ErrorCode.USER_NOT_FOUND)
```

### 네이밍
- 클래스: PascalCase
- 함수/변수: snake_case
- 상수: UPPER_SNAKE_CASE
- 테이블/컬럼: snake_case
- API 경로: kebab-case, 복수형 명사 (/users, /commutes, /routes, /cards)
- 비공개 속성은 `_` 접두사 (self._session)

### 타입과 비동기
- 모든 함수에 타입 힌트. mypy strict 통과가 기준
- router·service·clients 전부 async. 동기 I/O(requests, time.sleep) 금지
- `Any` 는 외부 API 응답을 파싱하는 clients/ 안에서만 허용하고, 밖으로 나갈 때는 Pydantic 모델로 바꾼다

## API 설계 원칙
- RESTful. 모든 응답은 JSON
- 인증이 필요한 API 는 Authorization: Bearer {supabaseAccessToken} 헤더 필수
- 데모 동선(사전 계산된 Route)은 게스트도 조회 가능. 저장·기록은 로그인 필수
- Swagger UI: http://localhost:8000/docs
- 응답 시간: 카드 조회 5초, 저장된 동선 경로 조회 1초. 신규 동선 샛길 생성은 10초 (사전 계산 권장)

## 환경 설정

### 로컬 실행
```bash
# 1. 로컬 PostGIS 도커 실행 (Supabase 대신 로컬에서 테스트할 때)
docker compose up -d

# 2. 의존성 설치
uv sync

# 3. .env 작성 후 마이그레이션 + 실행
uv run alembic upgrade head
uv run uvicorn app.main:app --reload

# 4. 데모 계정 생성 (Supabase Admin API + users 행). 이미 있으면 건너뛴다
uv run python -m scripts.seed_demo
```

### 환경변수 (.env)
```
DATABASE_URL=postgresql+asyncpg://...
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_JWT_AUDIENCE=authenticated
TMAP_APP_KEY=
ODSAY_API_KEY=
KMA_SERVICE_KEY=
LLM_API_KEY=
APP_ENV=dev
# scripts/seed_demo.py 전용. 앱 런타임은 읽지 않는다
SUPABASE_SERVICE_ROLE_KEY=
DEMO_EMAIL=
DEMO_PASSWORD=
```
- Settings 는 core/config.py 의 pydantic-settings 하나로 읽는다. `os.environ` 직접 접근 금지
- 키 값은 코드·테스트·로그·커밋 메시지 어디에도 쓰지 않는다

### Git 제외 파일
- `.env` 는 .gitignore 에 반드시 포함한다. `.env.example` 만 커밋한다

## 테스트

### 재현 테스트를 먼저 작성한다
- 결함을 고칠 때는 재현 테스트를 먼저 `test:` 로 커밋하고, 그다음 `fix:` 로 고친다
- 테스트 커밋 시점에는 그 테스트가 실패하는 것이 정상이다. 결함이 실재함을 기록으로 남기는 것이 목적이다
- 새 기능도 같은 순서: 기대 동작을 테스트로 먼저 쓰고, 통과시키는 코드를 쓴다

### 구조와 도구
- pytest + pytest-asyncio (`asyncio_mode = "auto"`)
- API 테스트는 httpx AsyncClient 로 app 을 직접 부른다. 서버를 띄우지 않는다
- DB 테스트는 docker compose 의 로컬 PostGIS 를 쓴다. 테스트마다 트랜잭션을 롤백한다 (conftest 의 db_session 픽스처)
- 외부 API(TMAP, LLM 등)는 clients/ 래퍼를 픽스처로 바꿔치기한다. 테스트에서 실제 네트워크 호출 금지
- JWT 테스트는 conftest 에서 테스트용 키 쌍을 만들어 서명하고, JWKS 조회를 그 공개키로 바꿔치기한다
- 테스트 이름: `test_{상황}_{기대결과}` (예: test_expired_token_returns_401)
- 지리 계산(녹지 %, 이벤트 점수)은 작은 고정 폴리곤으로 단위 테스트한다. 실제 OSM 데이터에 의존하는 테스트는 `@pytest.mark.integration` 으로 분리한다

```bash
uv run pytest                    # 단위 + API
uv run pytest -m integration     # OSM 데이터 필요
uv run ruff check . && uv run ruff format --check . && uv run mypy app
```

## Git 컨벤션

### 브랜치 전략
```
main              배포 브랜치 (병합되면 GitHub Actions 가 자동 배포한다)
develop           개발 통합 브랜치
feat/#{이슈번호}   기능 개발 (예: feat/#12)
fix/#{이슈번호}    버그 수정
docs/#{이슈번호}   문서 작업
```
이슈와 브랜치는 사람이 만든다. 작업은 항상 이슈 번호에 붙는다.

### 커밋 메시지
형식: `{타입}: {무엇을 했는지} - {왜 그렇게 했는지 또는 결과} (#이슈번호)`

타입: feat / fix / refactor / test / docs / chore

규칙
- 한국어 한 줄로 쓴다. 본문은 쓰지 않고, 서사는 이슈와 PR 본문에 둔다
- 요약만 쓰지 말고 판단 근거나 측정 결과를 뒤에 붙인다
  예: `feat: Supabase JWT 검증 의존성 추가 - JWKS 캐시로 요청당 조회 제거 (#3)`
- 숫자는 실측값만 쓴다. 추정치는 쓰지 않는다
- 결함 수정과 기능 추가는 커밋을 나눈다
- 공동 작성자(Co-Authored-By) 등 생성 도구 트레일러는 넣지 않는다

### 이슈 · PR 템플릿
- 이슈 본문 초안은 .github/ISSUE_TEMPLATE/ 의 양식을, PR 본문 초안은 .github/pull_request_template.md 의 양식을 그대로 따른다
- 이슈와 PR 생성은 사람이 한다. Claude는 양식을 채운 초안을 제안하는 데서 멈춘다

### 커밋·푸시·PR
커밋, 푸시, PR 생성은 사람이 직접 한다. Claude 는 변경 내용과 커밋 메시지, PR 본문 초안을 제안하는 데서 멈춘다.

## Claude 작업 규칙
- 작업 시작 전 이 파일과 해당 도메인의 기존 코드를 읽고 같은 구조를 따른다
- 새 엔드포인트 하나 = router + service + schemas + 테스트. 하나라도 빠지면 미완성이다
- 코드를 바꾼 뒤에는 `uv run pytest` 와 `uv run ruff check .` 를 돌려 결과를 보고한다. 돌리지 않고 "통과할 것"이라고 말하지 않는다
- 모르는 외부 API 응답 형식은 추측해서 파싱하지 않는다. 문서 링크나 실제 응답 예시를 요청한다
- .env, alembic 자동 생성 마이그레이션의 drop 문, main 브랜치는 건드리지 않는다
- 집·회사 좌표를 저장하는 코드, 도시 이름을 하드코딩한 코드는 작성하지 않는다. 요청받아도 이유를 말하고 대안을 제안한다