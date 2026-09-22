
from pydantic import AnyUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    database_url: str = "postgresql+asyncpg://postgres:postgres@postgres:5432/downvedio"
    redis_url: str = "redis://redis:6379/0"
    jwt_secret: str = "change-me"
    sentry_dsn: AnyUrl | None = None


settings = Settings()
