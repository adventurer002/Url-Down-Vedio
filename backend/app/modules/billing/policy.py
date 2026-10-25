from calendar import monthrange
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from zoneinfo import ZoneInfo

from app.core.exceptions import AppException

VERSION = "allowances-v1"
BEIJING = ZoneInfo("Asia/Shanghai")
PRODUCTS = {
    "monthly": {"name": "月会员", "price_fen": 1990, "asr_seconds": 18000, "ai_generations": 100},
    "asr_120": {"name": "120 分钟转写包", "price_fen": 990, "asr_seconds": 7200, "ai_generations": 0},
    "ai_100": {"name": "100 次 AI 包", "price_fen": 990, "asr_seconds": 0, "ai_generations": 100},
}
FREE_LIMITS = {"video_seconds": 1200, "file_bytes": 256 * 1024**2, "daily_egress_bytes": 1024**3, "download_concurrency": 1}
MEMBER_LIMITS = {"video_seconds": 3600, "file_bytes": 512 * 1024**2, "daily_egress_bytes": 5 * 1024**3, "download_concurrency": 3}
MAX_TEXT_CHARS = 60000
MAX_OUTPUT_TOKENS = 4096
MAX_PENDING_TASKS = 10


def now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def seconds(value):
    """One ceiling per complete audio, never per ASR chunk or retry."""
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0:
            raise InvalidOperation
        return int(amount.to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, TypeError, ValueError):
        raise AppException("DURATION_REQUIRED", "首版仅支持时长明确的非直播视频", 422) from None


def month_boundary(anchor, offset):
    local = aware(anchor).astimezone(BEIJING)
    year, month = divmod(local.year * 12 + local.month - 1 + offset, 12)
    return local.replace(year=year, month=month + 1, day=min(local.day, monthrange(year, month + 1)[1])).astimezone(timezone.utc)


def catalog():
    return {"version": VERSION, "currency": "CNY", "products": PRODUCTS,
            "trial": {"asr_seconds": 1800, "ai_generations": 5, "valid_days": 30},
            "addon_valid_days": 30, "rollover": False, "free_daily_downloads": 2,
            "free_limits": FREE_LIMITS, "member_limits": MEMBER_LIMITS,
            "max_text_chars": MAX_TEXT_CHARS, "max_output_tokens_per_call": MAX_OUTPUT_TOKENS,
            "max_total_output_tokens": 16384, "max_llm_calls": 16,
            "asr_rounding": "完整音频按秒向上取整；字幕及转写缓存不扣分钟",
            "cache_policy": "公共 AI 缓存首次获取扣 1 次；自己的相同结果免费",
            "refund_policy": "正常交付后不支持无理由退款；重复支付、未交付及依法应退款情形除外",
            "purchase_mode": "operator_grant_only", "real_payment_enabled": False,
            "worker_capacity_note": "账号额度不代表机器并行能力；当前 Solo 单 Worker 实际串行"}
