from pathlib import Path
from decimal import Decimal

from pydantic import Field, SecretStr
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / "backend" / ".env", PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8", extra="ignore"
    )

    APP_ENV: str = "development"
    AUTH_REQUIRED: bool = False
    ALLOWANCES_ENABLED: bool = True
    JWT_SECRET_KEY: str = "dev-secret-change-in-production"
    JWT_EXPIRE_MINUTES: int = Field(default=10080, gt=0)
    WECHAT_MP_MODE: Literal["mock", "verify_code", "parameterized_qr"] = "mock"
    WECHAT_MP_TOKEN: str = "videohub_wechat_token"
    WECHAT_MP_APP_ID: str = ""
    WECHAT_MP_APP_SECRET: SecretStr = SecretStr("")
    WECHAT_MP_STATIC_QR_URL: str = ""

    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    DATABASE_URL: str = "postgresql+psycopg://videohub_user:videohub_pass@localhost:5432/videohub_dev"
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    STORAGE_LOCAL_PATH: Path = Path("./storage")
    STORAGE_MAX_QUOTA_MB: int = Field(default=5120, gt=0)
    STORAGE_FILE_TTL_HOURS: int = Field(default=24, gt=0)
    # Trusted operator configuration; never accepted from HTTP request bodies.
    YTDLP_PROXY: SecretStr = SecretStr("")
    YTDLP_JS_RUNTIME: Literal["disabled", "deno"] = "disabled"
    YTDLP_DENO_PATH: str = ""
    DOWNLOAD_TIMEOUT_SECONDS: int = Field(default=600, gt=0)
    MAX_FILE_SIZE_MB: int = Field(default=512, gt=0)
    STORAGE_MIN_FREE_MB: int = Field(default=256, ge=0)
    TRANSCRIPT_CONTENT_VERSION: str = Field(default="v1", min_length=1, max_length=64)
    SUBTITLE_TIMEOUT_SECONDS: int = Field(default=45, gt=0)
    ASR_PROVIDER: Literal["auto", "openai_compatible", "qwen", "disabled", "mock"] = "auto"
    ASR_API_BASE_URL: str = "https://api.openai.com/v1"
    ASR_API_KEY: SecretStr = SecretStr("")
    ASR_MODEL: str = Field(default="whisper-1", min_length=1, max_length=128)
    ASR_TOTAL_TIMEOUT_SECONDS: int = Field(default=90, gt=0)
    ASR_MAX_UPLOAD_MB: int = Field(default=25, gt=0)
    ASR_MOCK_TEXT: str = "离线模拟转写内容（非真实识别结果）"
    LLM_PROVIDER: Literal["auto", "openai_compatible", "heuristic"] = "auto"
    LLM_API_BASE_URL: str = "https://api.openai.com/v1"
    LLM_API_KEY: SecretStr = SecretStr("")
    LLM_MODEL: str = Field(default="gpt-4o-mini", min_length=1, max_length=128)
    LLM_TIMEOUT_SECONDS: int = Field(default=90, gt=0)
    AI_TASK_TIMEOUT_SECONDS: int = Field(default=900, gt=0)
    LLM_CHUNK_CHARS: int = Field(default=12000, ge=256)
    LLM_MAX_CHUNKS: int = Field(default=64, ge=1, le=256)
    LLM_MAX_OUTPUT_TOKENS: int = Field(default=4096, ge=128, le=16384)
    SUMMARY_PROMPT_VERSION: str = Field(default="summary-v2", min_length=1, max_length=64)
    MINDMAP_PROMPT_VERSION: str = Field(default="mindmap-v2", min_length=1, max_length=64)
    MINDMAP_SCHEMA_VERSION: str = Field(default="tree-v1", min_length=1, max_length=32)
    LLM_INPUT_COST_PER_MILLION: Decimal | None = Field(default=None, ge=0)
    LLM_OUTPUT_COST_PER_MILLION: Decimal | None = Field(default=None, ge=0)

    @property
    def storage_path(self) -> Path:
        return (PROJECT_ROOT / self.STORAGE_LOCAL_PATH).resolve()


settings = Settings()
