"""All mutations serialize on the account row; callers own the transaction.

Tasks and media are accessed exclusively through their repository contracts.
"""
from datetime import timedelta
import hashlib
from uuid import UUID, uuid4

from sqlalchemy import case, func, select

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.auth.repository import UserRepository
from app.modules.billing.models import (Allowance, AllowanceAccount, AllowanceAllocation,
    AllowanceEvent, AllowanceGrant, AllowanceQuote, AllowanceReservation, AllowanceTransfer, DailyAllowance)
from app.modules.billing.policy import (BEIJING, FREE_LIMITS, MEMBER_LIMITS, PRODUCTS, VERSION,
    aware, month_boundary, now)


class AllowanceRepository:
    def __init__(self, sessions):
        self.sessions = sessions
        self.identities = UserRepository(sessions)

    def event(self, session, key, user_id, event, **detail):
        session.add(AllowanceEvent(key=key, user_id=user_id, event=event, detail=detail))

    def lock_existing(self, session, user_id):
        # Releases must remain possible after unsubscribe, disablement or expiry.
        return session.scalar(select(AllowanceAccount).where(AllowanceAccount.user_id == user_id).with_for_update())

    def lock_account(self, session, user_id):
        if settings.APP_ENV != "development" and settings.WECHAT_MP_MODE == "mock":
            raise AppException("AUTH_MISCONFIGURED", "生产额度服务不能使用模拟公众号身份", 503)
        if user_id is None:
            raise AppException("AUTH_REQUIRED", "请登录后使用额度服务", 401)
        identity = self.identities.billing_identity(session, user_id)
        if not identity or not identity["active"] or not identity["bound"]:
            raise AppException("ACCOUNT_NOT_BOUND", "请先通过公众号绑定产品账号", 403)
        if session.bind.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        fingerprint = hashlib.sha256((settings.WECHAT_MP_APP_ID + ":" + identity["openid"]).encode()).hexdigest()
        session.execute(insert(AllowanceAccount).values(user_id=user_id, identity_key=fingerprint,
            member_months=0).on_conflict_do_nothing(index_elements=[AllowanceAccount.user_id]))
        account = session.scalar(select(AllowanceAccount).where(AllowanceAccount.user_id == user_id).with_for_update())
        if account.trial_claimed_at is None and identity["subscribed"]:
            instant = now()
            account.trial_claimed_at = instant
            self.add_allowances(session, user_id, "trial:" + fingerprint, "trial", instant,
                                instant + timedelta(days=30), 1800, 5)
        session.flush()
        for lot in session.scalars(select(Allowance).where(Allowance.user_id == user_id, Allowance.expires_at <= now())):
            key = "expire:" + str(lot.id)
            if session.get(AllowanceEvent, key) is None:
                self.event(session, key, user_id, "expire", allowance_id=str(lot.id),
                           amount=lot.granted - lot.used - lot.held, held=lot.held,
                           expires_at=aware(lot.expires_at).isoformat())
        session.flush()
        return account, identity

    def add_allowances(self, session, user_id, key, source, start, end, asr, ai):
        for kind, amount in (("asr_seconds", asr), ("ai_generations", ai)):
            if amount:
                session.add(Allowance(id=uuid4(), user_id=user_id, source_key=key + ":" + kind,
                    source=source, kind=kind, granted=amount, used=0, held=0, starts_at=start, expires_at=end))
        self.event(session, key, user_id, "grant", source=source, asr_seconds=asr,
                   ai_generations=ai, starts_at=aware(start).isoformat(), expires_at=aware(end).isoformat())
        session.flush()

    @staticmethod
    def member(account):
        return account.member_until is not None and aware(account.member_until) > now()

    def limits(self, account):
        return dict(MEMBER_LIMITS if self.member(account) else FREE_LIMITS)

    def daily(self, session, user_id, day=None):
        day = day or now().astimezone(BEIJING).date()
        daily = session.get(DailyAllowance, (user_id, day))
        if daily is None:
            daily = DailyAllowance(user_id=user_id, day=day, downloads_used=0, downloads_held=0,
                                   egress_used=0, egress_held=0)
            session.add(daily)
            session.flush()
        return daily

    def available(self, session, user_id, kind):
        return list(session.scalars(select(Allowance).where(Allowance.user_id == user_id,
            Allowance.kind == kind, Allowance.starts_at <= now(), Allowance.expires_at > now())
            .order_by(Allowance.expires_at, case((Allowance.source == "trial", 0), else_=1), Allowance.id)))

    def balance(self, user_id):
        with self.sessions.begin() as session:
            account, identity = self.lock_account(session, user_id)
            lots = list(session.scalars(select(Allowance).where(Allowance.user_id == user_id).order_by(Allowance.expires_at)))
            totals = {}
            for kind in ("asr_seconds", "ai_generations"):
                totals[kind] = {"available": sum(x.granted - x.used - x.held for x in lots
                    if x.kind == kind and aware(x.starts_at) <= now() < aware(x.expires_at)
                    and (identity["subscribed"] or x.source != "trial")),
                    "held": sum(x.held for x in lots if x.kind == kind)}
            daily = self.daily(session, user_id)
            return {"version": VERSION, "member": self.member(account), "member_until": account.member_until,
                "subscribed": identity["subscribed"], "balances": totals, "limits": self.limits(account),
                "daily": {"date": daily.day, "downloads_used": daily.downloads_used,
                    "downloads_held": daily.downloads_held, "egress_used_bytes": daily.egress_used,
                    "egress_held_bytes": daily.egress_held},
                "allowances": [{"kind": x.kind, "source": x.source, "granted": x.granted,
                    "used": x.used, "held": x.held, "starts_at": x.starts_at, "expires_at": x.expires_at,
                    "expired": aware(x.expires_at) <= now()} for x in lots]}

    def ledger(self, user_id, limit=50, offset=0):
        with self.sessions() as session:
            rows = session.scalars(select(AllowanceEvent).where(AllowanceEvent.user_id == user_id)
                .order_by(AllowanceEvent.created_at.desc(), AllowanceEvent.key).limit(limit).offset(offset))
            return [{"id": x.key, "event": x.event, "detail": x.detail, "created_at": x.created_at} for x in rows]

    def grant(self, user_id, product, key, actor):
        if product not in PRODUCTS or not key or not actor:
            raise AppException("INVALID_GRANT", "需要有效商品、唯一发放键和操作人", 422)
        with self.sessions.begin() as session:
            account, _ = self.lock_account(session, user_id)
            existing = session.get(AllowanceGrant, key)
            if existing:
                if existing.user_id != user_id or existing.product != product:
                    raise AppException("IDEMPOTENCY_CONFLICT", "同一发放键对应不同请求", 409)
                return {"key": key, "product": product, "starts_at": existing.starts_at,
                        "expires_at": existing.expires_at, "duplicate": True}
            offer = PRODUCTS[product]
            start = now()
            if product == "monthly":
                if not self.member(account):
                    account.member_anchor, account.member_months = start, 0
                start = month_boundary(account.member_anchor, account.member_months)
                account.member_months += 1
                end = month_boundary(account.member_anchor, account.member_months)
                account.member_until = end
            else:
                end = start + timedelta(days=30)
            snapshot = {**offer, "version": VERSION, "currency": "CNY", "mode": "operator_grant"}
            session.add(AllowanceGrant(key=key, user_id=user_id, product=product, actor=actor,
                                      snapshot=snapshot, starts_at=start, expires_at=end))
            self.add_allowances(session, user_id, "manual:" + key, product, start, end,
                                offer["asr_seconds"], offer["ai_generations"])
            return {"key": key, "product": product, "starts_at": start, "expires_at": end, "duplicate": False}

    def save_quote(self, user_id, task_type, payload):
        with self.sessions.begin() as session:
            account, identity = self.lock_account(session, user_id)
            if not self.member(account) and not identity["subscribed"] and task_type in {"download", "audio_extraction"}:
                raise AppException("WECHAT_UNSUBSCRIBED", "免费权益需要保持关注公众号", 403)
            limits = self.limits(account)
            if task_type != "ai_question" and payload["duration_seconds"] > limits["video_seconds"]:
                raise AppException("VIDEO_TOO_LONG", "视频超过当前权益的时长限制", 422)
            recent = session.scalar(select(func.count()).select_from(AllowanceQuote).where(
                AllowanceQuote.user_id == user_id, AllowanceQuote.created_at > now() - timedelta(hours=1)))
            if recent >= 60:
                raise AppException("QUOTE_RATE_LIMITED", "报价过于频繁，请稍后再试", 429)
            from app.modules.ai.providers import get_llm_provider
            from app.modules.transcript.providers import get_asr_provider
            llm, asr = get_llm_provider(), get_asr_provider()
            if settings.APP_ENV != "development" and task_type.startswith("ai_") and llm.name == "heuristic":
                raise AppException("LLM_NOT_CONFIGURED", "生产 AI 服务需要配置真实模型", 503)
            quota = {"version": VERSION, "asr_seconds": payload["duration_seconds"] if task_type in
                     {"transcription", "ai_summary", "ai_mindmap"} else 0,
                     "ai_generations": int(task_type in {"ai_summary", "ai_mindmap", "ai_question"}), "limits": limits,
                     "llm_model": llm.model, "asr_model": asr.model,
                     "ai_limits": {"text_chars": 60000, "output_tokens_per_call": 4096, "total_output_tokens": 16384, "max_calls": 16},
                     "summary_version": settings.SUMMARY_PROMPT_VERSION,
                     "mindmap_version": settings.MINDMAP_PROMPT_VERSION + ":" + settings.MINDMAP_SCHEMA_VERSION}
            if payload.get("reuse_result"):
                quota["asr_seconds"], quota["ai_generations"] = 0, 0
            quote = AllowanceQuote(id=uuid4(), user_id=user_id, task_type=task_type,
                payload={**payload, "allowance": quota}, expires_at=now() + timedelta(minutes=15))
            session.add(quote)
            return {"quote_id": str(quote.id), "task_type": task_type, "maximum": quota,
                    "expires_at": quote.expires_at, "note": "排队不占用；执行前占用上限，字幕/转写缓存释放分钟，成功后核销"}

    def quote(self, user_id, quote_id):
        with self.sessions() as session:
            item = session.get(AllowanceQuote, quote_id)
            if item is None or item.user_id != user_id:
                raise AppException("QUOTE_NOT_FOUND", "报价不存在", 404)
            if item.task_id is None and aware(item.expires_at) <= now():
                raise AppException("QUOTE_EXPIRED", "报价已过期，请重新获取", 409)
            return item.task_type, {**item.payload, "quote_id": str(item.id)}, item.task_id

    def bind_quote(self, session, user_id, payload, task_type):
        self.lock_account(session, user_id)
        try:
            quote_id = UUID(payload["quote_id"])
        except (KeyError, ValueError, TypeError):
            raise AppException("QUOTE_REQUIRED", "请先 POST /billing/quotes，再 POST /billing/tasks 确认额度上限", 422) from None
        quote = session.scalar(select(AllowanceQuote).where(AllowanceQuote.id == quote_id,
            AllowanceQuote.user_id == user_id).with_for_update())
        if quote is None:
            raise AppException("QUOTE_NOT_FOUND", "报价不存在", 404)
        if quote.task_type != task_type or {k: v for k, v in payload.items() if k != "quote_id"} != quote.payload:
            raise AppException("QUOTE_MISMATCH", "任务与已确认报价不一致", 409)
        if quote.task_id is None and aware(quote.expires_at) <= now():
            raise AppException("QUOTE_EXPIRED", "报价已过期", 409)
        return quote

    def admit(self, session, task):
        account, identity = self.lock_account(session, task.user_id)
        if "allowance" not in task.input_payload:
            return "quote_required"
        snapshot = task.input_payload["allowance"]
        member = self.member(account)
        if not member and not identity["subscribed"] and task.task_type in {"download", "audio_extraction"}:
            return "entitlement"
        limits = self.limits(account)
        if task.task_type != "ai_question" and task.input_payload["duration_seconds"] > limits["video_seconds"]:
            return "entitlement"
        download = task.task_type in {"download", "audio_extraction"}
        slot = "download" if download else "ai"
        running = session.scalar(select(func.count()).select_from(AllowanceReservation).where(
            AllowanceReservation.user_id == task.user_id, AllowanceReservation.state == "held", AllowanceReservation.slot == slot))
        if running >= (limits["download_concurrency"] if download else 1):
            return "capacity"
        daily = self.daily(session, task.user_id)
        if download and not member and daily.downloads_used + daily.downloads_held >= 2:
            return "daily_downloads"
        amounts = {kind: snapshot[kind] for kind in ("asr_seconds", "ai_generations")}
        lots = {kind: self.available(session, task.user_id, kind) for kind in amounts}
        if not identity["subscribed"]:
            lots = {kind: [lot for lot in values if lot.source != "trial"] for kind, values in lots.items()}
        if any(sum(x.granted - x.used - x.held for x in lots[kind]) < amount for kind, amount in amounts.items()):
            return "allowance"
        reservation = AllowanceReservation(task_id=task.id, user_id=task.user_id, state="held", slot=slot,
            business_date=daily.day, free_download=int(download and not member), asr_seconds=0,
            snapshot={**snapshot, "limits": limits})
        session.add(reservation)
        session.flush()
        for kind, amount in amounts.items():
            for lot in lots[kind]:
                take = min(amount, lot.granted - lot.used - lot.held)
                if take:
                    lot.held += take
                    session.add(AllowanceAllocation(task_id=task.id, allowance_id=lot.id, amount=take))
                    amount -= take
                if amount == 0:
                    break
        daily.downloads_held += reservation.free_download
        self.event(session, "hold:" + str(task.id), task.user_id, "hold", task_id=str(task.id), **amounts)
        session.flush()
        return None

    def execution_limits(self, session, task_id):
        return session.get(AllowanceReservation, task_id).snapshot["limits"]

    def record_asr(self, session, task_id, duration):
        item = session.get(AllowanceReservation, task_id)
        if item and item.state == "held":
            if duration > item.snapshot["limits"]["video_seconds"]:
                raise AppException("VIDEO_TOO_LONG", "实际音频时长超过当前任务限制", 422)
            item.asr_seconds = min(duration, item.snapshot["asr_seconds"])
            key = "asr:" + str(task_id)
            if session.get(AllowanceEvent, key) is None:
                self.event(session, key, item.user_id, "asr_usage", measured_seconds=duration,
                    user_max_seconds=item.snapshot["asr_seconds"], platform_seconds=max(0, duration - item.snapshot["asr_seconds"]))
            session.flush()

    def finalize(self, session, task, success, owned=False):
        item = session.get(AllowanceReservation, task.id)
        if item is None or item.state != "held":
            return
        item.state = "settled" if success else "released"
        amounts = {"asr_seconds": item.asr_seconds if success and not owned else 0,
                   "ai_generations": item.snapshot["ai_generations"] if success and not owned else 0}
        item.settlement = dict(amounts)
        splits = list(session.scalars(select(AllowanceAllocation).where(AllowanceAllocation.task_id == task.id)))
        def allocation_order(split):
            lot = session.get(Allowance, split.allowance_id)
            return aware(lot.expires_at), int(lot.source != "trial"), str(lot.id)
        for split in sorted(splits, key=allocation_order):
            lot = session.get(Allowance, split.allowance_id)
            used = min(split.amount, amounts[lot.kind])
            amounts[lot.kind] -= used
            lot.held -= split.amount
            lot.used += used
            self.event(session, "allocation:" + str(task.id) + ":" + str(lot.id), task.user_id,
                "settle" if success else "release", allowance_id=str(lot.id), consumed=used,
                released=split.amount - used, expires_at=aware(lot.expires_at).isoformat(),
                release_expired=aware(lot.expires_at) <= now())
        daily = self.daily(session, task.user_id, item.business_date)
        daily.downloads_held -= item.free_download
        if success and not owned:
            daily.downloads_used += item.free_download
        self.event(session, "final:" + str(task.id), task.user_id, item.state,
                   task_id=str(task.id), already_owned=owned, **item.settlement)
        session.flush()

    def reserve_transfer(self, user_id, length):
        with self.sessions.begin() as session:
            account, _ = self.lock_account(session, user_id)
            daily = self.daily(session, user_id)
            if daily.egress_used + daily.egress_held + length > self.limits(account)["daily_egress_bytes"]:
                raise AppException("DAILY_TRAFFIC_EXCEEDED", "今日下载流量额度不足", 429)
            daily.egress_held += length
            item = AllowanceTransfer(id=uuid4(), user_id=user_id, day=daily.day, reserved=length)
            session.add(item)
            return item.id

    def finish_transfer(self, transfer_id, sent):
        with self.sessions.begin() as session:
            item = session.get(AllowanceTransfer, transfer_id)
            self.lock_existing(session, item.user_id)
            session.refresh(item)
            if item.sent is not None:
                return
            item.sent = min(item.reserved, max(0, sent))
            daily = self.daily(session, item.user_id, item.day)
            daily.egress_held -= item.reserved
            daily.egress_used += item.sent
