from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.billing.policy import aware, now
from app.modules.billing.repository import AllowanceRepository
from app.modules.download.models import DownloadDelivery
from app.modules.download.relay import TRANSFER_SECONDS, byte_range


@dataclass(frozen=True)
class DeliveryLease:
    token: UUID
    source: dict
    maximum: int
    transfer_id: UUID
    charged_now: bool
    free_download_charged: bool


class DeliveryRepository:
    def __init__(self, sessions):
        self.sessions = sessions
        self.billing = AllowanceRepository(sessions)

    @staticmethod
    def active_count(session, user_id):
        return session.scalar(select(func.count()).select_from(DownloadDelivery).where(
            DownloadDelivery.user_id == user_id, DownloadDelivery.active_until > now()))

    @staticmethod
    def register(session, task, source, expires_at):
        session.add(DownloadDelivery(task_id=task.id, user_id=task.user_id, source=source,
            expires_at=expires_at, charged=False))

    @staticmethod
    def owned(session, identifier, user_id, *, lock=False):
        statement = select(DownloadDelivery).where(DownloadDelivery.task_id == identifier,
            DownloadDelivery.user_id == user_id)
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        item = session.scalar(statement)
        if item is None:
            raise AppException("DELIVERY_NOT_FOUND", "下载会话不存在", 404)
        return item

    def get(self, identifier, user_id):
        with self.sessions() as session:
            item = self.owned(session, identifier, user_id)
            return {"expires_at": item.expires_at, "fallback_quote_id": item.fallback_quote_id,
                    "fallback_task_id": item.fallback_task_id,
                    "active": bool(item.active_until and aware(item.active_until) > now()),
                    "started": item.charged,
                    "last_error": item.last_error or ("DELIVERY_EXPIRED" if aware(item.expires_at) <= now() else None),
                    "delivery_mode": item.source["delivery_mode"]}

    def validate_access(self, identifier, user_id):
        """Fail before native browser handoff, without reserving or charging a transfer."""
        with self.sessions.begin() as session:
            account, identity = self.billing.lock_account(session, user_id)
            item = self.owned(session, identifier, user_id)
            limits = self.billing.limits(account)
            if item.fallback_task_id:
                raise AppException("DELIVERY_FALLBACK_STARTED", "已改为生成成品，请查看对应任务", 409)
            if ((item.active_until and aware(item.active_until) > now()) or
                    self.billing.active_downloads(session, user_id) >= limits["download_concurrency"]):
                raise AppException("DELIVERY_BUSY", "下载连接正在使用，请稍后重试", 409)
            if (item.source["duration_seconds"] > limits["video_seconds"] or
                    item.source["size_bytes"] > min(limits["file_bytes"], settings.MAX_FILE_SIZE_MB * 1024**2)):
                raise AppException("DELIVERY_ENTITLEMENT_CHANGED", "当前权益不支持此文件，请重新确认下载", 403)
            if not self.billing.member(account):
                if not identity["subscribed"]:
                    raise AppException("WECHAT_UNSUBSCRIBED", "免费权益需要保持关注公众号", 403)
                daily = self.billing.daily(session, user_id)
                if not item.charged and daily.downloads_used + daily.downloads_held >= 2:
                    raise AppException("DAILY_DOWNLOADS_EXCEEDED", "今日免费下载次数已用完", 429)
            if item.source["delivery_mode"] != "direct":
                daily = self.billing.daily(session, user_id)
                limit = limits["daily_egress_bytes"]
                if limit is not None and daily.egress_used + daily.egress_held + item.source["size_bytes"] > limit:
                    raise AppException("DAILY_TRAFFIC_EXCEEDED", "今日下载流量额度不足", 429)

    def reserve(self, identifier, user_id, range_header):
        with self.sessions.begin() as session:
            account, identity = self.billing.lock_account(session, user_id)
            item = self.owned(session, identifier, user_id, lock=True)
            instant = now()
            if aware(item.expires_at) <= instant:
                raise AppException("DELIVERY_EXPIRED", "下载来源已过期，请生成成品下载", 409)
            if item.fallback_task_id:
                raise AppException("DELIVERY_FALLBACK_STARTED", "已改为生成成品，请查看对应任务", 409)
            limits = self.billing.limits(account)
            active = self.billing.active_downloads(session, user_id)
            if ((item.active_until and aware(item.active_until) > instant) or
                    active >= limits["download_concurrency"]):
                raise AppException("DELIVERY_BUSY", "下载连接正在使用，请稍后重试", 409)
            maximum = min(limits["file_bytes"], settings.MAX_FILE_SIZE_MB * 1024**2)
            if item.source["size_bytes"] > maximum or item.source["duration_seconds"] > limits["video_seconds"]:
                raise AppException("DELIVERY_ENTITLEMENT_CHANGED", "当前权益不支持此文件，请重新确认下载", 403)
            if not self.billing.member(account) and not identity["subscribed"]:
                raise AppException("WECHAT_UNSUBSCRIBED", "免费权益需要保持关注公众号", 403)
            start, end = byte_range(range_header, item.source["size_bytes"])
            transfer = self.billing.reserve_transfer_in_transaction(session, account,
                0 if item.source.get("delivery_mode") == "direct" else end - start + 1)
            charged_now = not item.charged
            free_download_charged = False
            if charged_now:
                free_download_charged = self.billing.charge_delivery(session, account)
                item.charged = True
            token = uuid4()
            item.active_token, item.active_until = token, instant + timedelta(seconds=TRANSFER_SECONDS + 30)
            item.last_error = None
            return DeliveryLease(token, dict(item.source), maximum, transfer, charged_now, free_download_charged)

    def finish(self, identifier, user_id, lease, sent, error=None):
        with self.sessions.begin() as session:
            self.billing.lock_existing(session, user_id)
            item = self.owned(session, identifier, user_id, lock=True)
            if item.active_token != lease.token:
                return
            self.billing.finish_transfer_in_transaction(session, lease.transfer_id, sent,
                refund_download=lease.free_download_charged and sent == 0 and error is not None)
            if lease.charged_now and sent == 0 and error is not None:
                item.charged = False
            item.active_token = item.active_until = None
            item.last_error = error

    def select_fallback_quote(self, identifier, user_id, quote_id):
        with self.sessions.begin() as session:
            self.billing.lock_account(session, user_id)
            item = self.owned(session, identifier, user_id, lock=True)
            if item.active_until and aware(item.active_until) > now():
                raise AppException("DELIVERY_BUSY", "请先结束当前下载，再生成成品", 409)
            if item.fallback_quote_id is None or (item.fallback_task_id is None and
                    not self.billing.quote_usable(session, item.fallback_quote_id)):
                item.fallback_quote_id = quote_id
            return item.fallback_quote_id

    def bind_fallback(self, session, identifier, user_id, quote_id, task_id=None):
        item = self.owned(session, identifier, user_id, lock=True)
        if item.fallback_quote_id != quote_id:
            raise AppException("QUOTE_MISMATCH", "成品任务与原下载会话不一致", 409)
        if item.active_until and aware(item.active_until) > now():
            raise AppException("DELIVERY_BUSY", "请先结束当前下载，再生成成品", 409)
        if task_id:
            item.fallback_task_id = task_id
        return item.fallback_task_id

    def fallback_prepaid(self, session, identifier, user_id, task_id):
        item = self.owned(session, identifier, user_id)
        if item.fallback_task_id != task_id:
            raise AppException("DELIVERY_NOT_FOUND", "成品任务未绑定下载会话", 409)
        return item.charged

    def settle_fallback(self, session, identifier, user_id, task_id):
        self.fallback_prepaid(session, identifier, user_id, task_id)
        self.owned(session, identifier, user_id).charged = True
