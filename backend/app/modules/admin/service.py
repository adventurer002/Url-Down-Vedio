import asyncio
from datetime import timezone
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import AppException
from app.modules.audit.repository import AuditRepository, operator_text
from app.modules.auth.repository import UserRepository
from app.modules.billing.policy import BEIJING, now, PRODUCTS
from app.modules.billing.repository import AllowanceRepository
from app.modules.tasks.repository import TaskRepository
from app.modules.media.repository import MediaRepository
from app.modules.video.repository import VideoRepository


class AdminService:
    def __init__(self, sessions, audit=None):
        self.sessions = sessions
        self.users = UserRepository(sessions)
        self.allowances = AllowanceRepository(sessions)
        self.audit = audit or AuditRepository()
        self.tasks = TaskRepository(sessions)

    def list_users(self, limit=20, offset=0, q=None, status=None, subscribed=None, member=None):
        instant = now()
        with self.sessions() as session:
            items, total = self.users.admin_list(session, limit, offset, q, status, subscribed,
                member, self.allowances.member_user_ids(instant))
            memberships = self.allowances.membership_summaries(session, [item["id"] for item in items], instant)
            return {"items": [{**item, **memberships[item["id"]]} for item in items],
                    "total": total, "limit": limit, "offset": offset}

    def user_detail(self, user_id):
        with self.sessions() as session:
            user = self.users.admin_detail(session, user_id)
            return {**user, **self.allowances.membership_summaries(session, [user_id], now())[user_id]}

    def user_allowances(self, user_id, limit=20, offset=0):
        with self.sessions() as session:
            user = self.users.admin_detail(session, user_id)
            return self.allowances.admin_snapshot(session, user_id, user["status"] == "active",
                                                  user["is_subscribed"], limit, offset)

    def user_ledger(self, user_id, limit=50, offset=0):
        with self.sessions() as session:
            self.users.admin_detail(session, user_id)
            return self.allowances.admin_ledger(session, user_id, limit, offset)

    def _mutate(self, actor, action, target_type, target_id, reason, key, request_id, request_data, operation, *, operator=None):
        reason, key = operator_text(reason, 500), operator_text(key, 128)
        if key.startswith(("cleanup-result:", "attempt_")):
            raise AppException("INVALID_ADMIN_OPERATION", "幂等键使用了系统保留前缀", 422)
        try:
            return self._execute_mutation(actor, action, target_type, target_id, reason, key,
                                          request_id, request_data, operation, operator=operator)
        except Exception as error:
            # Business writes have rolled back. Failed attempts use separate keys,
            # so transient failures do not permanently consume the operation key.
            try:
                with self.sessions.begin() as session:
                    if actor:
                        self.users.require_admin(session, actor.id, actor.auth_version)
                    committed = self.audit.bootstrap_event(session, key)
                    if committed and (committed.actor_user_id == (actor.id if actor else None)
                        and committed.actor_label == (str(actor.id) if actor else operator_text(operator, 128))
                        and committed.action == action and committed.target_type == target_type
                        and committed.target_id == str(target_id) and committed.reason == reason
                        and committed.request_data == request_data):
                        # A lost COMMIT acknowledgement is not proof of failure.
                        # The atomic success audit is authoritative on a fresh connection.
                        return {**committed.result, "duplicate": True}
                    uncertain = isinstance(error, (SQLAlchemyError, TimeoutError))
                    self.audit.record_action(session, actor_id=actor.id if actor else None,
                        actor_label=str(actor.id) if actor else operator_text(operator, 128), action=action,
                        target_type=target_type, target_id=target_id, reason=reason, key="attempt_" + uuid4().hex,
                        request_id=request_id, request_data={**request_data, "operation_key": key}, changes={},
                        result={"error_code": "DATABASE_OUTCOME_UNKNOWN" if uncertain else error.code if isinstance(error, AppException) else "ADMIN_OPERATION_FAILED"},
                        outcome="unknown" if uncertain else "failed")
            except Exception:
                pass  # No business mutation committed; preserve the original safe failure.
            raise

    def _execute_mutation(self, actor, action, target_type, target_id, reason, key, request_id, request_data, operation, *, operator=None):
        reason, key = operator_text(reason, 500), operator_text(key, 128)
        actor_id = actor.id if actor else None
        label = str(actor.id) if actor else operator_text(operator, 128)
        with self.sessions.begin() as session:
            self.audit.lock_key(session, key)
            if actor:
                self.users.require_admin(session, actor.id, actor.auth_version)
            existing = self.audit.bootstrap_event(session, key)
            if existing:
                if (existing.actor_user_id != actor_id or existing.actor_label != label or existing.action != action
                        or existing.target_type != target_type or existing.target_id != str(target_id)
                        or existing.reason != reason or existing.request_data != request_data):
                    raise AppException("IDEMPOTENCY_CONFLICT", "同一幂等键对应不同操作", 409)
                return {**existing.result, "duplicate": True}
            result, changes, outcome = operation(session)
            response = self.audit.record_action(session, actor_id=actor_id, actor_label=label,
                action=action, target_type=target_type, target_id=target_id, reason=reason, key=key,
                request_id=request_id, request_data=request_data, changes=changes, result=result, outcome=outcome)
            return {**response, "duplicate": False}

    def change_user_status(self, actor, user_id, status, expected_status, expected_version, reason, key, request_id):
        if status not in {"active", "disabled"} or expected_status not in {"active", "disabled"}:
            raise AppException("INVALID_ADMIN_OPERATION", "账号状态无效", 422)

        def change(session):
            # Same lock order as task admission: allowance account -> user.
            self.allowances.lock_existing(session, user_id)
            before, after = self.users.change_status(session, user_id, status, expected_status, expected_version)
            return ({"user_id": str(user_id), **after}, {"before": before, "after": after},
                    "succeeded" if before != after else "unchanged")

        return self._mutate(actor, "user.status", "user", user_id, reason, key, request_id,
            {"status": status, "expected_status": expected_status, "expected_version": expected_version}, change)

    def grant_preview(self, user_id, product):
        with self.sessions() as session:
            user = self.users.admin_detail(session, user_id)
            if user["status"] != "active" or user["last_login_at"] is None:
                raise AppException("ACCOUNT_NOT_BOUND", "请先通过公众号绑定活跃产品账号", 403)
            return self.allowances.grant_preview(session, user_id, product)

    def grant_user(self, actor, user_id, product, reason, key, request_id, *, operator=None):
        if product not in PRODUCTS:
            raise AppException("INVALID_GRANT", "需要有效商品", 422)
        key = operator_text(key, 128)
        if actor is None:
            operator = operator_text(operator, 128)

        def grant(session):
            self.users.admin_detail(session, user_id)
            result = self.allowances.grant_in_transaction(session, user_id, product, key,
                                                         str(actor.id) if actor else operator)
            if result["duplicate"]:
                raise AppException("LEGACY_GRANT_EXISTS", "已有历史发放记录，请查询流水，不补造审计或重复发放", 409)
            result.update(mode="operator_grant", payment_verified=False)
            return result, {"product": product, "starts_at": result["starts_at"],
                            "expires_at": result["expires_at"], "offer": dict(PRODUCTS[product])}, "succeeded"

        return self._mutate(actor, "allowance.grant", "user", user_id, reason, key, request_id,
                            {"product": product}, grant, operator=operator)

    async def wake_after_grant(self, user_id, result):
        from app.modules.billing.service import resume_queued
        try:
            async with asyncio.timeout(6):
                dispatched = await resume_queued(TaskRepository(self.sessions), user_id)
            return {**result, "dispatched": dispatched, "dispatch_pending": False}
        except (AppException, TimeoutError):
            # The grant has committed. Durable queued rows and reconciliation
            # handle broker outages; never report a failed purchase or grant again.
            return {**result, "dispatched": None, "dispatch_pending": True}

    @staticmethod
    def validate_interval(since, until):
        if any(value is not None and value.utcoffset() is None for value in (since, until)):
            raise AppException("TIMEZONE_REQUIRED", "时间筛选必须包含时区", 422)
        if since is not None and until is not None and since >= until:
            raise AppException("INVALID_TIME_RANGE", "开始时间必须早于结束时间", 422)
        return (since.astimezone(timezone.utc) if since else None, until.astimezone(timezone.utc) if until else None)

    def list_tasks(self, limit=20, offset=0, user_id=None, status=None, task_type=None, since=None, until=None):
        since, until = self.validate_interval(since, until)
        with self.sessions() as session:
            page = self.tasks.admin_list(session, limit, offset, user_id, status, task_type, since, until)
            usage = self.allowances.admin_task_usage(session, [item["task_id"] for item in page["items"]])
            videos = VideoRepository(self.sessions).admin_metadata(session, [item["video_id"] for item in page["items"] if item["video_id"]])
            for item in page["items"]:
                item["allowance"] = usage[item["task_id"]]
                item["video"] = videos.get(item["video_id"])
            return page

    def task_detail(self, task_id):
        with self.sessions() as session:
            task = self.tasks.admin_detail(session, task_id)
            task["allowance"] = self.allowances.admin_task_usage(session, [task_id])[task_id]
            task["video"] = VideoRepository(self.sessions).admin_metadata(session, [task["video_id"]]).get(task["video_id"]) if task["video_id"] else None
            return task

    def cancel_task(self, actor, task_id, reason, key, request_id):
        def cancel(session):
            prior = {}
            def capture(task):
                prior.update(status=task.status, cancel_requested=task.cancel_requested)
            task = self.tasks.request_cancel_in_transaction(session, task_id, capture)
            state = {"status": task.status, "cancel_requested": task.cancel_requested}
            return ({"task_id": str(task_id), **state}, {"before": prior, "after": state},
                    "unchanged" if prior == state else "accepted" if task.status in {"running", "processing"} else "succeeded")

        return self._mutate(actor, "task.cancel", "task", task_id, reason, key, request_id, {}, cancel)

    def list_audit(self, limit=50, offset=0, actor_id=None, action=None, target_type=None, target_id=None,
                   outcome=None, since=None, until=None):
        since, until = self.validate_interval(since, until)
        with self.sessions() as session:
            page = self.audit.list(session, limit, offset, actor_id, action, target_type, target_id, outcome, since, until)
            ids = []
            for item in page["items"]:
                if item["target_type"] == "task":
                    try:
                        ids.append(UUID(item["target_id"]))
                    except ValueError:
                        pass
            statuses = self.tasks.status_map(session, ids)
            for item in page["items"]:
                if item["target_type"] == "task":
                    item["current_task_status"] = statuses.get(item["target_id"])
            return page

    def overview(self):
        instant = now()
        day_start = instant.astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.sessions() as session:
            return {"as_of": instant, "business_date": day_start.date(), "timezone": "Asia/Shanghai",
                "users": self.users.admin_statistics(session), "tasks": self.tasks.admin_statistics(session, day_start.astimezone(timezone.utc)),
                "allowances": self.allowances.admin_statistics(session, instant),
                "media": MediaRepository(self.sessions).admin_statistics(session, instant),
                "real_payment_enabled": False, "revenue": None, "supplier_cost": None,
                "worker": {"pool": "solo", "concurrency": 1, "liveness": "not_measured"}}

    def begin_cleanup(self, actor, reason, key, request_id):
        return self._mutate(actor, "storage.cleanup.request", "storage", "local", reason, key, request_id, {},
            lambda session: ({"operation_id": str(uuid4()), "state": "accepted"}, {}, "accepted"))

    def cleanup_completion(self, operation_id):
        with self.sessions() as session:
            event = self.audit.bootstrap_event(session, "cleanup-result:" + operation_id)
            if event and (event.action != "storage.cleanup.finish" or event.request_data.get("operation_id") != operation_id):
                raise AppException("IDEMPOTENCY_CONFLICT", "清理结果记录冲突，请检查审计", 409)
            return event.result if event else None

    def finish_cleanup(self, actor, started, reason, request_id, result, error_code=None):
        key = "cleanup-result:" + started["operation_id"]
        with self.sessions.begin() as session:
            self.audit.lock_key(session, key)
            existing = self.audit.bootstrap_event(session, key)
            if existing:
                if existing.action != "storage.cleanup.finish" or existing.request_data.get("operation_id") != started["operation_id"]:
                    raise AppException("IDEMPOTENCY_CONFLICT", "清理结果记录冲突，请检查审计", 409)
                return existing.result
            # The operation may have removed files before the actor was revoked.
            # Recording its outcome must still be possible after revocation.
            return self.audit.record_action(session, actor_id=actor.id, actor_label=str(actor.id),
                action="storage.cleanup.finish", target_type="storage", target_id="local", reason=reason,
                key=key, request_id=request_id, request_data={"operation_id": started["operation_id"],
                    "request_audit_id": started["audit_id"]}, changes={},
                result={"operation_id": started["operation_id"], "state": "failed_or_partial" if error_code else "completed",
                        **({"error_code": error_code} if error_code else result)}, outcome="failed" if error_code else "succeeded")

    async def cleanup(self, actor, reason, key, request_id, cleaner):
        from app.modules.tasks.service import database_call
        started = await database_call(self.begin_cleanup, actor, reason, key, request_id)
        if started["duplicate"]:
            completion = await database_call(self.cleanup_completion, started["operation_id"])
            return {**(completion or {**started, "requires_review": True}), "duplicate": True}
        try:
            result = await asyncio.to_thread(cleaner.run)
            result = {name: result[name] for name in (
                "files", "temporary_directories", "orphan_files", "freed_bytes", "used_bytes")}
        except Exception as error:
            code = error.code if isinstance(error, AppException) else "STORAGE_CLEANUP_FAILED"
            await database_call(self.finish_cleanup, actor, started, reason, request_id, {}, code)
            if isinstance(error, AppException):
                raise
            raise AppException("STORAGE_CLEANUP_FAILED", "文件清理失败，请查询操作审计", 503) from None
        try:
            return await database_call(self.finish_cleanup, actor, started, reason, request_id, result)
        except AppException:
            # Filesystem changes cannot be rolled back with the audit transaction.
            return {**started, "state": "outcome_unrecorded", "result": result, "requires_review": True}

    def bootstrap(self, user_id, actor, reason, key):
        actor, reason, key = operator_text(actor, 128), operator_text(reason, 500), operator_text(key, 128)
        if key.startswith(("cleanup-result:", "attempt_")):
            raise AppException("INVALID_ADMIN_OPERATION", "幂等键使用了系统保留前缀", 422)
        with self.sessions.begin() as session:
            self.users.lock_admin_initialization(session)
            existing = self.audit.bootstrap_event(session, key)
            if existing:
                if (existing.action != "admin.bootstrap" or existing.target_id != str(user_id)
                        or existing.actor_label != actor or existing.reason != reason):
                    raise AppException("IDEMPOTENCY_CONFLICT", "同一幂等键对应不同操作", 409)
                return {"user_id": str(user_id), "audit_id": str(existing.id), "duplicate": True}
            self.users.initialize_admin(session, user_id)
            audit_id = self.audit.record_bootstrap(session, user_id, actor, reason, key, "cli_" + uuid4().hex)
            return {"user_id": str(user_id), "audit_id": str(audit_id), "duplicate": False}
