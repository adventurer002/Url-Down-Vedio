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
        # A status change can commit while this transaction waits for the account
        # lock. Refresh identity after admission serialization, never trust the pre-lock read.
        identity = self.identities.billing_identity(session, user_id)
        if not identity or not identity["active"] or not identity["bound"]:
            raise AppException("ACCOUNT_NOT_BOUND", "请先通过公众号绑定产品账号", 403)
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

    @staticmethod
    def member_user_ids(instant):
        """Read-only membership filter contract for administrative user queries."""
        return select(AllowanceAccount.user_id).where(AllowanceAccount.member_until > instant)

    def membership_summaries(self, session, user_ids, instant):
        result = {identifier: {"member": False, "member_until": None} for identifier in user_ids}
        if user_ids:
            for account in session.scalars(select(AllowanceAccount).where(AllowanceAccount.user_id.in_(user_ids))):
                result[account.user_id] = {"member": bool(account.member_until and aware(account.member_until) > instant),
                                           "member_until": account.member_until}
        return result

    def admin_snapshot(self, session, user_id, active, subscribed, limit=20, offset=0):
        """Pure SELECTs, including for disabled/unbound users. Never call lock_account/daily."""
        instant = now()
        account = session.get(AllowanceAccount, user_id)
        balances = {}
        for kind in ("asr_seconds", "ai_generations"):
            eligible = (Allowance.starts_at <= instant) & (Allowance.expires_at > instant)
            if not subscribed:
                eligible = eligible & (Allowance.source != "trial")
            available, held = session.execute(select(
                func.coalesce(func.sum(case((eligible, Allowance.granted - Allowance.used - Allowance.held), else_=0)), 0),
                func.coalesce(func.sum(Allowance.held), 0)).where(
                    Allowance.user_id == user_id, Allowance.kind == kind)).one()
            balances[kind] = {"available": available, "held": held, "usable": available if active else 0}
        conditions = [Allowance.user_id == user_id]
        total = session.scalar(select(func.count()).select_from(Allowance).where(*conditions))
        lots = session.scalars(select(Allowance).where(*conditions)
            .order_by(Allowance.expires_at, Allowance.id).limit(limit).offset(offset))
        items = []
        for lot in lots:
            started, expired = aware(lot.starts_at) <= instant, aware(lot.expires_at) <= instant
            usable = active and started and not expired and (subscribed or lot.source != "trial")
            items.append({"id": lot.id, "kind": lot.kind, "source": lot.source,
                "granted": lot.granted, "used": lot.used, "held": lot.held,
                "usable": lot.granted - lot.used - lot.held if usable else 0,
                "starts_at": lot.starts_at, "expires_at": lot.expires_at,
                "expired": expired, "future": not started})
        day = instant.astimezone(BEIJING).date()
        daily = session.get(DailyAllowance, (user_id, day))
        member = bool(account and account.member_until and aware(account.member_until) > instant)
        return {"version": VERSION, "as_of": instant, "account_exists": account is not None,
            "account_active": active, "subscribed": subscribed, "member": member,
            "member_until": account.member_until if account else None,
            "trial_claimed_at": account.trial_claimed_at if account else None,
            "balances": balances, "limits": dict(MEMBER_LIMITS if member else FREE_LIMITS),
            "daily": {"date": day, "downloads_used": daily.downloads_used if daily else 0,
                "downloads_held": daily.downloads_held if daily else 0,
                "egress_used_bytes": daily.egress_used if daily else 0,
                "egress_held_bytes": daily.egress_held if daily else 0},
            "lots": {"items": items, "total": total, "limit": limit, "offset": offset}}

    def admin_ledger(self, session, user_id, limit=50, offset=0):
        condition = AllowanceEvent.user_id == user_id
        total = session.scalar(select(func.count()).select_from(AllowanceEvent).where(condition))
        events = session.scalars(select(AllowanceEvent).where(condition)
            .order_by(AllowanceEvent.created_at.desc(), AllowanceEvent.key).limit(limit).offset(offset))
        return {"items": [{"id": event.key, "event": event.event, "detail": event.detail,
                            "created_at": event.created_at} for event in events],
                "total": total, "limit": limit, "offset": offset}

    def admin_task_usage(self, session, task_ids):
        result = {identifier: None for identifier in task_ids}
        if task_ids:
            for item in session.scalars(select(AllowanceReservation).where(AllowanceReservation.task_id.in_(task_ids))):
                result[item.task_id] = {"state": item.state, "business_date": item.business_date,
                    "maximum": {name: item.snapshot.get(name, 0) for name in ("asr_seconds", "ai_generations")},
                    "measured_asr_seconds": item.asr_seconds, "settlement": item.settlement}
        return result

    def admin_statistics(self, session, instant):
        result = {"valid_memberships": session.scalar(select(func.count()).select_from(AllowanceAccount)
            .where(AllowanceAccount.member_until > instant)), "usage": {}}
        for kind in ("asr_seconds", "ai_generations"):
            used, held = session.execute(select(func.coalesce(func.sum(Allowance.used), 0),
                func.coalesce(func.sum(Allowance.held), 0)).where(Allowance.kind == kind)).one()
            result["usage"][kind] = {"consumed": used, "held": held}
        return result

    def grant(self, user_id, product, key, actor):
        """Internal legacy contract; operator entry points use AdminService for audit."""
        with self.sessions.begin() as session:
            return self.grant_in_transaction(session, user_id, product, key, actor)

    def grant_preview(self, session, user_id, product):
        if product not in PRODUCTS:
            raise AppException("INVALID_GRANT", "需要有效商品", 422)
        account = session.get(AllowanceAccount, user_id)
        start = now()
        if product == "monthly":
            if account and self.member(account):
                start = month_boundary(account.member_anchor, account.member_months)
                end = month_boundary(account.member_anchor, account.member_months + 1)
            else:
                end = month_boundary(start, 1)
        else:
            end = start + timedelta(days=30)
        return {"product": product, "offer": dict(PRODUCTS[product]), "starts_at": start, "expires_at": end,
                "member_until_before": account.member_until if account else None,
                "mode": "operator_grant", "payment_verified": False,
                "note": "预览不锁定时间；最终以发放事务结果为准，人工发放不是支付凭证"}

    def grant_in_transaction(self, session, user_id, product, key, actor):
        if product not in PRODUCTS or not key or not actor:
            raise AppException("INVALID_GRANT", "需要有效商品、唯一发放键和操作人", 422)
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
        clips = task_type in {"ai_clip_plan", "clips_export"}
        if clips:
            from app.modules.clips.service import enabled
            enabled()
        with self.sessions.begin() as session:
            account, identity = self.lock_account(session, user_id)
            if not self.member(account) and not identity["subscribed"] and task_type in {"download", "audio_extraction"}:
                raise AppException("WECHAT_UNSUBSCRIBED", "免费权益需要保持关注公众号", 403)
            limits = self.limits(account)
            text_only = payload.get("source_kind") == "transcript" and task_type in {"ai_summary", "ai_mindmap", "ai_clip_plan"}
            if task_type != "ai_question" and not text_only and payload["duration_seconds"] > limits["video_seconds"]:
                raise AppException("VIDEO_TOO_LONG", "视频超过当前权益的时长限制", 422)
            if payload.get("source_kind") == "upload" and payload["size_bytes"] > min(limits["file_bytes"], settings.MAX_FILE_SIZE_MB * 1024**2):
                raise AppException("FILE_TOO_LARGE", "文件超过当前权益的体积限制", 413)
            recent = session.scalar(select(func.count()).select_from(AllowanceQuote).where(
                AllowanceQuote.user_id == user_id, AllowanceQuote.created_at > now() - timedelta(hours=1)))
            if recent >= 60:
                raise AppException("QUOTE_RATE_LIMITED", "报价过于频繁，请稍后再试", 429)
            from app.modules.ai.providers import get_llm_provider
            from app.modules.transcript.providers import get_asr_provider
            from app.modules.model_config.runtime import capture, llm as configured_llm, asr as configured_asr, TASK_USE
            model_config=capture(session) if task_type in TASK_USE else None
            if model_config:
                payload={**payload,"model_config":model_config}
                if task_type=="ai_clip_plan" and payload.get("clip_spec",{}).get("model")!=configured_llm("clip_analysis",model_config,self.sessions).model:
                    raise AppException("MODEL_CONFIGURATION_CHANGED","分析模型已更新，请重新获取方案",409)
                if task_type=="clips_export" and payload.get("timing_model")!=configured_asr("clip_timing",model_config,self.sessions).model:
                    raise AppException("MODEL_CONFIGURATION_CHANGED","定位模型已更新，请重新获取方案",409)
                llm=configured_llm(TASK_USE.get(task_type,"summary") if task_type not in {"transcription","clips_export"} else "summary",model_config,self.sessions)
                asr=None if text_only or task_type=="clips_export" else configured_asr("transcription",model_config,self.sessions)
            else:
                llm = get_llm_provider()
                asr = None if text_only or task_type == "clips_export" else get_asr_provider()
            if settings.APP_ENV != "development" and task_type.startswith("ai_") and llm.name == "heuristic":
                raise AppException("LLM_NOT_CONFIGURED", "生产 AI 服务需要配置真实模型", 503)
            quota = {"version": VERSION, "asr_seconds": payload["duration_seconds"] if task_type in
                     {"transcription", "ai_summary", "ai_mindmap", "ai_clip_plan"} and not text_only else 0,
                     "ai_generations": int(task_type in {"ai_summary", "ai_mindmap", "ai_question", "ai_clip_plan"}), "limits": limits,
                     "llm_model": llm.model, "asr_model": asr.model if asr else "none",
                     "ai_limits": {"text_chars": 60000, "output_tokens_per_call": 4096, "total_output_tokens": 16384, "max_calls": 16},
                     "summary_version": settings.SUMMARY_PROMPT_VERSION,
                     "mindmap_version": settings.MINDMAP_PROMPT_VERSION + ":" + settings.MINDMAP_SCHEMA_VERSION}
            if clips:
                quota["clip_limits"] = {"analysis_timeout": settings.CLIPS_ANALYSIS_TIMEOUT_SECONDS,
                    "export_timeout": settings.CLIPS_EXPORT_TIMEOUT_SECONDS,
                    "render_timeout": settings.CLIPS_RENDER_TIMEOUT_SECONDS,
                    "max_temp_bytes": settings.CLIPS_EXPORT_MAX_TEMP_MB * 1024**2,
                    "max_export_seconds": settings.CLIPS_EXPORT_MAX_TOTAL_SECONDS,
                    "timing_timeout": settings.CLIPS_TIMING_TIMEOUT_SECONDS,
                    "max_timing_seconds": settings.CLIPS_TIMING_MAX_AUDIO_SECONDS,
                    "max_daily_exports": settings.CLIPS_MAX_DAILY_EXPORTS,
                    "max_daily_plan_exports": settings.CLIPS_MAX_DAILY_PLAN_EXPORTS}
                if task_type == "clips_export" and not payload.get("reuse_result") and not payload.get("reuse_assets"):
                    from app.modules.tasks.repository import TaskRepository
                    counts = TaskRepository(self.sessions).clip_render_counts(session, user_id, payload["plan_id"])
                    if counts[0] >= quota["clip_limits"]["max_daily_exports"] or counts[1] >= quota["clip_limits"]["max_daily_plan_exports"]:
                        raise AppException("CLIP_RENDER_LIMIT", "今日切片渲染次数已达上限", 429)
            if payload.get("reuse_result"):
                quota["asr_seconds"], quota["ai_generations"] = 0, 0
            if payload.get("source_selection"):
                quota["source_selection"] = {**payload["source_selection"],
                    "max_source_bytes": min(limits["file_bytes"], settings.MAX_FILE_SIZE_MB * 1024**2)}
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
        text_only = task.input_payload.get("source_kind") == "transcript" and task.task_type in {"ai_summary", "ai_mindmap", "ai_clip_plan"}
        if task.task_type != "ai_question" and not text_only and task.input_payload["duration_seconds"] > limits["video_seconds"]:
            return "entitlement"
        if task.input_payload.get("source_kind") == "upload" and task.input_payload["size_bytes"] > min(limits["file_bytes"], settings.MAX_FILE_SIZE_MB * 1024**2):
            return "entitlement"
        download = task.task_type in {"download", "audio_extraction"}
        slot = "download" if download else "ai"
        running = session.scalar(select(func.count()).select_from(AllowanceReservation).where(
            AllowanceReservation.user_id == task.user_id, AllowanceReservation.state == "held", AllowanceReservation.slot == slot))
        if running >= (limits["download_concurrency"] if download else 1):
            return "capacity"
        if task.task_type == "clips_export" and task.input_payload.get("_clip_execution_mode") != "cache" and not task.input_payload.get("reuse_result") and not task.input_payload.get("reuse_assets"):
            from app.modules.tasks.repository import TaskRepository
            counts = TaskRepository(self.sessions).clip_render_counts(session, task.user_id, task.input_payload["plan_id"])
            clips = snapshot["clip_limits"]
            if counts[0] >= clips["max_daily_exports"] or counts[1] >= clips["max_daily_plan_exports"]:
                return "clip_render_limit"
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
