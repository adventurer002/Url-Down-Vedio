
from pydantic import AnyUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    database_url: str = "postgresql+asyncpg://postgres:postgres@postgres:5432/downvedio"
    redis_url: str = "redis://redis:6379/0"
    jwt_secret: str = "change-me"
    sentry_dsn: AnyUrl | None = None

    # Phase3: media pipeline
    storage_backend: str = "local"
    storage_dir: str = "/tmp/downvedio-media"
    worker_tmp_dir: str = "/tmp/downvedio-tmp"
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_access_key: str = ""
    oss_secret_key: str = ""
    file_ttl_hours_free: int = 24
    file_ttl_hours_member: int = 168
    download_time_limit_seconds: int = 1800
    download_max_retries: int = 2

    # Phase4: auth + limits
    ip_rate_auth_per_min: int = 10
    ip_rate_parse_anon_per_min: int = 10
    ip_rate_parse_user_per_min: int = 60


settings = Settings()
