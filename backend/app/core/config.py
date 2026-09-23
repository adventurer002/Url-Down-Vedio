
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    database_url: str = "postgresql+asyncpg://postgres:postgres@postgres:5432/downvedio"
    redis_url: str = "redis://redis:6379/0"
    jwt_secret: str = "change-me"
    sentry_dsn: str = ""

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

    # Phase7: AI pipeline
    llm_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("LLM_BASE_URL", "DEEPSEEK_BASE_URL"),
    )
    llm_api_key: str = Field(
        default="", validation_alias=AliasChoices("LLM_API_KEY", "DEEPSEEK_API_KEY")
    )
    llm_model: str = Field(
        default="deepseek-flash", validation_alias=AliasChoices("LLM_MODEL", "DEEPSEEK_MODEL")
    )
    llm_prompt_version: str = "v1"
    whisper_model: str = "small"
    asr_cost_cents_per_min: int = 2
    llm_cost_cents_per_1k_tokens: int = 1

    # Phase5: frontend联调
    cors_origins: str = "http://localhost:5173,http://localhost:5199"

    # Phase8: payments (each provider independently enabled)
    stripe_enabled: bool = False
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_success_url: str = "http://localhost:5199/pay/success"
    stripe_cancel_url: str = "http://localhost:5199/pay/cancel"
    wechat_enabled: bool = False
    wechat_mchid: str = ""
    wechat_appid: str = ""
    wechat_serial_no: str = ""
    wechat_private_key: str = ""
    wechat_apiv3_key: str = ""
    wechat_notify_url: str = ""
    alipay_enabled: bool = False
    alipay_app_id: str = ""
    alipay_private_key: str = ""
    alipay_public_key: str = ""
    alipay_notify_url: str = ""
    alipay_return_url: str = "http://localhost:5199/pay/success"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
