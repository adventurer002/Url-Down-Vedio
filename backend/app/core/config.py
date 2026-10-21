from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    DATABASE_URL: str = "postgresql+psycopg://videohub_user:videohub_pass@localhost:5432/videohub_dev"
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    STORAGE_LOCAL_PATH: Path = Path("./storage")
    STORAGE_MAX_QUOTA_MB: int = Field(default=5120, gt=0)
    STORAGE_FILE_TTL_HOURS: int = Field(default=24, gt=0)
    DOWNLOAD_TIMEOUT_SECONDS: int = Field(default=600, gt=0)
    MAX_FILE_SIZE_MB: int = Field(default=512, gt=0)
    STORAGE_MIN_FREE_MB: int = Field(default=256, ge=0)

    @property
    def storage_path(self) -> Path:
        return (PROJECT_ROOT / self.STORAGE_LOCAL_PATH).resolve()


settings = Settings()
