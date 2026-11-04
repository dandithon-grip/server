from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수와 .env 에서 읽는 애플리케이션 설정.

    필드 이름이 곧 환경변수 이름이다 (대소문자 무시). 기본값이 없는 필드가 비어 있으면
    앱 시작 시점에 실패한다. 설정은 이 객체로만 읽고 os.environ 을 직접 읽지 않는다.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    supabase_url: str
    supabase_jwt_audience: str = "authenticated"
    # 공개 anon 키. Storage 게이트웨이가 apikey 헤더로 요구한다.
    # service role 키는 런타임 설정에 두지 않는다 (scripts/seed_demo.py 전용)
    supabase_anon_key: str = ""
    # 기록 사진이 올라가는 비공개 버킷 이름 (F-11). 프론트의 업로드 대상과 같아야 한다
    supabase_record_bucket: str = "records"
    default_city: str

    tmap_app_key: str = ""
    odsay_api_key: str = ""
    kma_service_key: str = ""
    airkorea_service_key: str = ""
    llm_api_key: str = ""

    app_env: Literal["dev", "test", "prod"] = "dev"

    @property
    def supabase_jwks_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"


@lru_cache
def get_settings() -> Settings:
    """Settings 를 한 번만 만들어 재사용한다."""
    return Settings()
