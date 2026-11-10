"""Audit writes join the caller's transaction; no independent commit or update API."""
import hashlib
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, text

from app.core.exceptions import AppException
from app.modules.audit.models import AuditEvent


def operator_text(value, maximum):
    if not isinstance(value, str):
        raise AppException("INVALID_ADMIN_OPERATION", "操作人、原因或幂等键无效", 422)
    value = value.strip()
    if not value or len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AppException("INVALID_ADMIN_OPERATION", "操作人、原因或幂等键无效", 422)
    return value


class AuditRepository:
    def model_release_metadata(self, session, release_ids):
        """Read publication context without exposing request bodies or credentials."""
        if not release_ids:
            return {}
        events = session.scalars(select(AuditEvent).where(
            AuditEvent.action == "model.publish",
            AuditEvent.outcome == "succeeded",
            AuditEvent.result["release_id"].as_string().in_(release_ids),
        ))
        return {event.result["release_id"]: {
            "actor_id": str(event.actor_user_id) if event.actor_user_id else None,
            "actor_label": event.actor_label,
            "reason": event.reason,
            "version": event.result.get("version"),
        } for event in events}

    @staticmethod
    def serialize(event):
        return {name: getattr(event, name) for name in (
            "id", "actor_kind", "actor_label", "actor_user_id", "action", "target_type", "target_id",
            "reason", "request_id", "idempotency_key", "outcome", "changes", "request_data", "result", "created_at")}

    def list(self, session, limit, offset, actor_id=None, action=None, target_type=None, target_id=None,
             outcome=None, since=None, until=None):
        conditions = []
        for column, value in ((AuditEvent.actor_user_id, actor_id), (AuditEvent.action, action),
                              (AuditEvent.target_type, target_type), (AuditEvent.target_id, target_id),
                              (AuditEvent.outcome, outcome)):
            if value is not None:
                conditions.append(column == value)
        if since is not None:
            conditions.append(AuditEvent.created_at >= since)
        if until is not None:
            conditions.append(AuditEvent.created_at < until)
        total = session.scalar(select(func.count()).select_from(AuditEvent).where(*conditions))
        events = session.scalars(select(AuditEvent).where(*conditions)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit).offset(offset))
        return {"items": [self.serialize(event) for event in events], "total": total, "limit": limit, "offset": offset}

    def lock_key(self, session, key):
        if session.bind.dialect.name == "postgresql":
            lock = int.from_bytes(hashlib.sha256(("admin-operation:" + key).encode()).digest()[:8], signed=True)
            session.execute(select(func.pg_advisory_xact_lock(lock)))
        elif session.bind.dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        else:
            raise AppException("ADMIN_DATABASE_UNSUPPORTED", "当前数据库不支持安全管理写入", 503)

    def record_action(self, session, *, actor_id, actor_label, action, target_type, target_id,
                      reason, key, request_id, request_data, changes, result, outcome="succeeded"):
        identifier = uuid4()
        response = jsonable_encoder({**result, "audit_id": str(identifier)})
        session.add(AuditEvent(id=identifier, actor_kind="user" if actor_id else "operator",
            actor_user_id=actor_id, actor_label=actor_label, action=action, target_type=target_type,
            target_id=str(target_id), reason=reason, idempotency_key=key, request_id=request_id,
            request_data=jsonable_encoder(request_data), changes=jsonable_encoder(changes),
            result=response, outcome=outcome))
        session.flush()
        return response

    def bootstrap_event(self, session, key):
        return session.scalar(select(AuditEvent).where(AuditEvent.idempotency_key == key))

    def record_bootstrap(self, session, user_id, actor, reason, key, request_id):
        # Explicit field allowlist: never copy user identity or request payloads.
        event = AuditEvent(actor_kind="operator", actor_label=actor, actor_user_id=None,
            action="admin.bootstrap", target_type="user", target_id=str(user_id), reason=reason,
            request_id=request_id, idempotency_key=key, outcome="succeeded",
            changes={"role": {"before": "user", "after": "admin"}})
        session.add(event)
        session.flush()
        return event.id
