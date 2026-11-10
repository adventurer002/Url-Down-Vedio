from datetime import timedelta
import hashlib, hmac
from uuid import UUID, uuid4
from sqlalchemy import select, func, or_, and_
from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.admin.service import AdminService
from app.modules.billing.policy import now, aware
from app.modules.model_config.models import (
    ModelResource,
    ModelRevision,
    ModelCredential,
    ModelRelease,
    ModelConfigState,
    ModelProbe,
)
from app.modules.model_config.security import endpoint, encrypt
from app.modules.model_config.runtime import USES
from app.modules.tasks.repository import TaskRepository

ADAPTERS = {
    "openai_chat": {"label": "OpenAI 兼容文本接口", "family": "llm"},
    "openai_asr": {"label": "OpenAI 兼容语音转写", "family": "asr"},
    "qwen_asr": {"label": "千问语音转写", "family": "asr"},
}


class ModelConfigService:
    def __init__(self, sessions):
        self.sessions = sessions

    def state(self, session, lock=False):
        if lock:
            if session.bind.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            session.execute(
                insert(ModelConfigState)
                .values(id=1, version=0)
                .on_conflict_do_nothing(index_elements=[ModelConfigState.id])
            )
        query = select(ModelConfigState).where(ModelConfigState.id == 1)
        return session.scalar(query.with_for_update() if lock else query)

    def revision(self, session, identifier, kind=None, live=True):
        try:
            identifier = UUID(str(identifier))
        except ValueError, TypeError:
            raise AppException(
                "MODEL_CONFIG_NOT_FOUND", "配置版本编号无效", 422
            ) from None
        revision = session.get(ModelRevision, identifier)
        head = session.get(ModelResource, revision.resource_id) if revision else None
        if not head or (kind and head.kind != kind) or (live and head.archived):
            raise AppException("MODEL_CONFIG_NOT_FOUND", "配置不存在或已归档", 404)
        return head, revision

    def latest(self, session, head):
        return session.scalar(
            select(ModelRevision).where(
                ModelRevision.resource_id == head.id,
                ModelRevision.version == head.version,
            )
        )

    def mutate(self, actor, action, target, body, request_id, operation, data=None):
        payload = (
            data
            if data is not None
            else body.model_dump(mode="json", exclude={"reason", "key"})
        )
        return AdminService(self.sessions).model_operation(
            actor,
            "model." + action,
            target,
            body.reason,
            body.key,
            request_id,
            payload,
            operation,
        )

    def save(self, actor, body, request_id, kind):
        data = body.model_dump(mode="json", exclude={"reason", "key", "api_key"})
        if kind == "connection":
            data["key_changed"] = bool(body.api_key and body.api_key.get_secret_value())
            if data["key_changed"]:
                data["key_fingerprint"] = hmac.new(
                    settings.MODEL_CONFIG_ENCRYPTION_KEY.get_secret_value().encode(),
                    body.api_key.get_secret_value().encode(),
                    hashlib.sha256,
                ).hexdigest()
        target = str(body.id) if body.id else "new-" + kind

        def operation(session):
            self.state(session, True)
            head = session.get(ModelResource, body.id) if body.id else None
            if body.id and (not head or head.kind != kind or head.archived):
                raise AppException("MODEL_CONFIG_NOT_FOUND", "配置不存在或已归档", 404)
            if (head.version if head else 0) != body.expected_version:
                raise AppException(
                    "MODEL_CONFIG_CONFLICT", "配置已变化，请刷新后重试", 409
                )
            if not head:
                head = ModelResource(
                    id=uuid4(), kind=kind, name=body.name, version=0, archived=False
                )
                session.add(head)
                session.flush()
            previous = self.latest(session, head) if head.version else None
            if kind == "connection":
                base = endpoint(body.base_url)
                credential_id = previous.payload["credential_id"] if previous else None
                if body.api_key and body.api_key.get_secret_value():
                    credential = ModelCredential(
                        id=uuid4(),
                        ciphertext=encrypt(body.api_key.get_secret_value()),
                        revoked=False,
                    )
                    session.add(credential)
                    credential_id = str(credential.id)
                if not credential_id:
                    raise AppException(
                        "MODEL_KEY_REQUIRED", "首次创建服务连接需要 API Key", 422
                    )
                payload = {
                    "adapter": body.adapter,
                    "base_url": base,
                    "credential_id": credential_id,
                }
            else:
                _, connection = self.revision(
                    session, body.connection_revision, "connection"
                )
                adapter = connection.payload["adapter"]
                if not body.model_id.strip():
                    raise AppException("MODEL_ID_REQUIRED", "请填写模型 ID", 422)
                payload = {
                    "connection_revision": str(connection.id),
                    "family": ADAPTERS[adapter]["family"],
                    "model_id": body.model_id,
                    "timeout": body.timeout,
                    "max_tokens": body.max_tokens,
                    "max_upload_mb": body.max_upload_mb,
                    "input_price": str(body.input_price)
                    if body.input_price is not None
                    else None,
                    "output_price": str(body.output_price)
                    if body.output_price is not None
                    else None,
                }
            head.name, head.version = body.name, head.version + 1
            revision = ModelRevision(
                id=uuid4(), resource_id=head.id, version=head.version, payload=payload
            )
            session.add(revision)
            session.flush()
            return (
                {
                    "id": str(head.id),
                    "revision_id": str(revision.id),
                    "version": head.version,
                },
                {"version": head.version, "kind": kind},
                "succeeded",
            )

        return self.mutate(
            actor, "save_" + kind, target, body, request_id, operation, data
        )

    def probe(self, actor, body, request_id):
        def operation(session):
            self.state(session, True)
            _, revision = self.revision(session, body.revision_id, "model")
            self.ensure_credential(session, revision)
            active_ids = TaskRepository(self.sessions).active_model_test_ids(session)
            # Include short-lived reservations between audited acceptance and task creation.
            active = session.scalars(
                select(ModelProbe).where(
                    or_(
                        ModelProbe.task_id.in_(active_ids),
                        and_(
                            ModelProbe.task_id.is_(None),
                            ModelProbe.created_at > now() - timedelta(minutes=2),
                        ),
                    )
                )
            ).all()
            if any(
                p.revision_id == revision.id and p.phase == body.phase for p in active
            ):
                raise AppException(
                    "MODEL_TEST_IN_PROGRESS", "此版本的同类测试已在排队或执行中", 409
                )
            if len(active) >= 3:
                raise AppException(
                    "MODEL_TEST_QUEUE_FULL",
                    "模型测试最多同时排队或执行三项，请等待现有测试完成",
                    429,
                )
            recent = session.scalar(
                select(func.count())
                .select_from(ModelProbe)
                .where(
                    ModelProbe.user_id == actor.id,
                    ModelProbe.created_at > now() - timedelta(hours=1),
                )
            )
            if recent >= 20:
                raise AppException(
                    "MODEL_TEST_RATE_LIMIT", "每小时最多测试二十次，请稍后再试", 429
                )
            probe = ModelProbe(
                id=uuid4(),
                revision_id=revision.id,
                phase=body.phase,
                user_id=actor.id,
                created_at=now(),
            )
            session.add(probe)
            session.flush()
            return {"probe_id": str(probe.id)}, {"phase": body.phase}, "accepted"

        return self.mutate(actor, "test", body.revision_id, body, request_id, operation)

    def bind_probe(self, session, probe_id, user_id, task_id=None):
        probe = session.scalar(
            select(ModelProbe)
            .where(ModelProbe.id == UUID(str(probe_id)))
            .with_for_update()
        )
        if not probe or probe.user_id != user_id:
            raise AppException("MODEL_TEST_NOT_FOUND", "模型测试不存在", 404)
        if probe.task_id:
            return probe.task_id
        if aware(probe.created_at) <= now() - timedelta(minutes=2):
            raise AppException(
                "MODEL_TEST_EXPIRED", "测试预约已过期，请重新发起测试", 409
            )
        if task_id:
            probe.task_id = task_id
        return None

    def probe_spec(self, identifier):
        with self.sessions() as session:
            probe = session.get(ModelProbe, UUID(str(identifier)))
            if not probe:
                raise AppException("MODEL_TEST_NOT_FOUND", "模型测试不存在", 404)
            return {
                "revision_id": str(probe.revision_id),
                "phase": probe.phase,
                "family": session.get(ModelRevision, probe.revision_id).payload[
                    "family"
                ],
                "user_id": probe.user_id,
            }

    def ensure_credential(self, session, revision):
        _, connection = self.revision(
            session, revision.payload["connection_revision"], "connection"
        )
        credential = session.get(
            ModelCredential, UUID(connection.payload["credential_id"])
        )
        if not credential or credential.revoked:
            raise AppException("MODEL_CREDENTIAL_REVOKED", "此模型的密钥已撤销", 409)
        return connection, credential

    def validate_bindings(self, session, bindings):
        if set(bindings) != set(USES) or not bindings["summary"]:
            raise AppException(
                "MODEL_BINDINGS_INVALID", "请分配默认总结模型并完整确认全部功能", 422
            )
        tasks = TaskRepository(self.sessions)
        for use, identifier in bindings.items():
            if not identifier:
                if use not in {"transcription", "clip_timing"} and not (
                    use == "clip_analysis" and not settings.CLIPS_ENABLED
                ):
                    raise AppException(
                        "MODEL_BINDINGS_INVALID", "文本功能需要分配模型", 422
                    )
                if use == "clip_timing" and settings.CLIPS_ENABLED:
                    raise AppException(
                        "MODEL_BINDINGS_INVALID", "剪辑功能需配置精确时间定位模型", 422
                    )
                continue
            _, revision = self.revision(session, identifier, "model")
            connection, _ = self.ensure_credential(session, revision)
            family = "asr" if use in {"transcription", "clip_timing"} else "llm"
            if revision.payload["family"] != family:
                raise AppException(
                    "MODEL_CAPABILITY_MISMATCH", "模型类型与功能不匹配", 422
                )
            if use == "clip_timing" and connection.payload["adapter"] != "openai_asr":
                raise AppException(
                    "MODEL_TIMESTAMPS_REQUIRED", "精确定位需要真实分段时间戳", 422
                )
            probes = session.scalars(
                select(ModelProbe)
                .where(
                    ModelProbe.revision_id == revision.id,
                    ModelProbe.phase == "compatibility",
                )
                .order_by(ModelProbe.created_at.desc(), ModelProbe.id.desc())
                .limit(20)
            ).all()
            result = (
                tasks.model_probe_result(session, probes[0].task_id) if probes else None
            )
            valid = bool(
                result
                and result["status"] == "completed"
                and use in result["result"].get("capabilities", [])
            )
            if not valid:
                raise AppException(
                    "MODEL_TEST_REQUIRED", "分配前请先通过对应业务兼容测试", 409
                )

    def publish(self, actor, body, request_id):
        def operation(session):
            if not settings.MODEL_CONFIG_ENABLED:
                raise AppException(
                    "MODEL_CONFIG_DISABLED", "请先启用服务端模型管理配置", 503
                )
            state = self.state(session, True)
            if state.version != body.expected_version:
                raise AppException(
                    "MODEL_CONFIG_CONFLICT", "线上配置已变化，请刷新后重试", 409
                )
            if body.release_id:
                old = session.get(ModelRelease, body.release_id)
                if not old:
                    raise AppException("MODEL_CONFIG_NOT_FOUND", "历史版本不存在", 404)
                bindings = old.bindings
            else:
                bindings = body.bindings
            self.validate_bindings(session, bindings)
            release = ModelRelease(id=uuid4(), bindings=bindings)
            session.add(release)
            session.flush()
            before = str(state.release_id) if state.release_id else None
            state.release_id, state.version = release.id, state.version + 1
            return (
                {"release_id": str(release.id), "version": state.version},
                {"before": before, "after": str(release.id)},
                "succeeded",
            )

        return self.mutate(actor, "publish", "active", body, request_id, operation)

    def archive(self, actor, identifier, body, request_id):
        def operation(session):
            state = self.state(session, True)
            head = session.get(ModelResource, identifier)
            if not head:
                raise AppException("MODEL_CONFIG_NOT_FOUND", "配置不存在", 404)
            if head.version != body.expected_version:
                raise AppException(
                    "MODEL_CONFIG_CONFLICT", "配置已变化，请刷新后重试", 409
                )
            release = (
                session.get(ModelRelease, state.release_id)
                if state.release_id
                else None
            )
            for ref in release.bindings.values() if release else []:
                if not ref:
                    continue
                model_head, model = self.revision(session, ref, "model", live=False)
                connection_head, _ = self.revision(
                    session,
                    model.payload["connection_revision"],
                    "connection",
                    live=False,
                )
                if identifier in {model_head.id, connection_head.id}:
                    raise AppException(
                        "MODEL_IN_USE", "请先替换功能分配，再归档此配置", 409
                    )
            head.archived = True
            return (
                {"id": str(head.id), "archived": True},
                {"archived": True},
                "succeeded",
            )

        return self.mutate(actor, "archive", identifier, body, request_id, operation)

    def revoke(self, actor, identifier, body, request_id):
        def operation(session):
            state = self.state(session, True)
            expected = getattr(body, "expected_version", None)
            if expected is not None and expected != state.version:
                raise AppException(
                    "MODEL_CONFIG_CONFLICT",
                    "功能分配已变化，请重新查看密钥影响范围",
                    409,
                )
            credential = session.get(ModelCredential, identifier)
            if not credential:
                raise AppException("MODEL_CONFIG_NOT_FOUND", "密钥版本不存在", 404)
            credential.revoked = True
            return (
                {"credential_id": str(identifier), "revoked": True},
                {"revoked": True},
                "succeeded",
            )

        return self.mutate(actor, "revoke_key", identifier, body, request_id, operation)

    def credential_impact(self, identifier):
        with self.sessions() as session:
            credential = session.get(ModelCredential, identifier)
            if not credential:
                raise AppException("MODEL_CONFIG_NOT_FOUND", "密钥版本不存在", 404)
            state = self.state(session)
            release = (
                session.get(ModelRelease, state.release_id)
                if state and state.release_id
                else None
            )
            bindings = release.bindings if release else {}
            connections = session.scalars(
                select(ModelRevision).where(
                    ModelRevision.payload["credential_id"].as_string()
                    == str(identifier)
                )
            ).all()
            models = session.scalars(
                select(ModelRevision).where(
                    ModelRevision.payload["connection_revision"]
                    .as_string()
                    .in_([str(c.id) for c in connections])
                )
            ).all()
            heads = {
                r.id: r
                for r in session.scalars(
                    select(ModelResource).where(
                        ModelResource.id.in_(
                            [r.resource_id for r in [*connections, *models]]
                        )
                    )
                )
            }

            def item(revision):
                head = heads[revision.resource_id]
                return {
                    "id": str(revision.id),
                    "name": head.name,
                    "version": revision.version,
                    "archived": head.archived,
                    "active_uses": [
                        use for use, ref in bindings.items() if ref == str(revision.id)
                    ],
                }

            return {
                "credential_id": str(identifier),
                "revoked": credential.revoked,
                "version": state.version if state else 0,
                "connections": [item(r) for r in connections],
                "models": [item(r) for r in models],
            }

    def catalog(self):
        from sqlalchemy import or_

        with self.sessions() as session:
            state = self.state(session)
            releases = session.scalars(
                select(ModelRelease).order_by(ModelRelease.created_at.desc()).limit(30)
            ).all()
            active = next(
                (item for item in releases if state and item.id == state.release_id),
                None,
            )
            active_ids = (
                [UUID(ref) for ref in active.bindings.values() if ref] if active else []
            )
            active_models = (
                session.scalars(
                    select(ModelRevision).where(ModelRevision.id.in_(active_ids))
                ).all()
                if active_ids
                else []
            )
            keep_ids = active_ids + [
                UUID(item.payload["connection_revision"]) for item in active_models
            ]
            heads = session.scalars(
                select(ModelResource).order_by(ModelResource.name).limit(500)
            ).all()
            ranked = select(
                ModelRevision.id.label("id"),
                func.row_number()
                .over(
                    partition_by=ModelRevision.resource_id,
                    order_by=ModelRevision.version.desc(),
                )
                .label("position"),
            ).subquery()
            revisions = session.scalars(
                select(ModelRevision)
                .join(ranked, ranked.c.id == ModelRevision.id)
                .where(
                    ModelRevision.resource_id.in_([head.id for head in heads]),
                    or_(ranked.c.position <= 50, ModelRevision.id.in_(keep_ids)),
                )
                .order_by(ModelRevision.version.desc())
            ).all()
            # Readiness checks need the exact connection versions referenced by
            # visible model drafts, even after many subsequent connection edits.
            present_ids = {item.id for item in revisions}
            referenced_connections = {
                UUID(item.payload["connection_revision"])
                for item in revisions
                if "connection_revision" in item.payload
            }
            missing = referenced_connections - present_ids
            if missing:
                revisions.extend(
                    session.scalars(
                        select(ModelRevision).where(
                            ModelRevision.id.in_(missing),
                            ModelRevision.resource_id.in_([head.id for head in heads]),
                        )
                    ).all()
                )
            credential_ids = [
                UUID(item.payload["credential_id"])
                for item in revisions
                if "credential_id" in item.payload
            ]
            credentials = (
                {
                    item.id: item
                    for item in session.scalars(
                        select(ModelCredential).where(
                            ModelCredential.id.in_(credential_ids)
                        )
                    )
                }
                if credential_ids
                else {}
            )
            grouped = {head.id: [] for head in heads}
            for revision in revisions:
                payload = dict(revision.payload)
                if "credential_id" in payload:
                    credential = credentials.get(UUID(payload["credential_id"]))
                    payload.update(
                        has_key=bool(credential and not credential.revoked),
                        revoked=bool(credential and credential.revoked),
                    )
                grouped[revision.resource_id].append(
                    {
                        "id": str(revision.id),
                        "version": revision.version,
                        "created_at": revision.created_at,
                        **payload,
                    }
                )
            resources = [
                {
                    "id": str(head.id),
                    "kind": head.kind,
                    "name": head.name,
                    "version": head.version,
                    "archived": head.archived,
                    "revisions": grouped[head.id],
                }
                for head in heads
            ]
            # Every visible revision retains its latest result for each phase. A global
            # history limit must never erase a model's publication eligibility in the UI.
            ranked_probes = select(
                ModelProbe.id.label("id"),
                func.row_number()
                .over(
                    partition_by=(ModelProbe.revision_id, ModelProbe.phase),
                    order_by=(ModelProbe.created_at.desc(), ModelProbe.id.desc()),
                )
                .label("position"),
            ).subquery()
            active_tests = TaskRepository(self.sessions).active_model_test_ids(session)
            probe_rows = session.scalars(
                select(ModelProbe)
                .join(ranked_probes, ranked_probes.c.id == ModelProbe.id)
                .where(
                    or_(
                        and_(
                            ranked_probes.c.position == 1,
                            ModelProbe.revision_id.in_([r.id for r in revisions]),
                        ),
                        ModelProbe.task_id.in_(active_tests),
                    )
                )
                .order_by(ModelProbe.created_at.desc(), ModelProbe.id.desc())
            ).all()
            results = TaskRepository(self.sessions).model_probe_results(
                session, [probe.task_id for probe in probe_rows]
            )
            probes = [
                {
                    "id": str(probe.id),
                    "revision_id": str(probe.revision_id),
                    "phase": probe.phase,
                    "created_at": probe.created_at,
                    "task_id": str(probe.task_id) if probe.task_id else None,
                    **(
                        results.get(probe.task_id)
                        or (
                            {"status": "pending", "result": {}}
                            if aware(probe.created_at) > now() - timedelta(minutes=2)
                            else {
                                "status": "failed",
                                "result": {},
                                "error_code": "MODEL_TEST_NOT_CREATED",
                            }
                        )
                    ),
                }
                for probe in probe_rows
            ]
            from app.modules.model_config.models import ModelCall
            from app.modules.audit.repository import AuditRepository
            from sqlalchemy import case

            release_metadata = AuditRepository().model_release_metadata(
                session, [str(item.id) for item in releases]
            )
            from app.modules.auth.repository import UserRepository

            actor_names = UserRepository(self.sessions).display_names(
                session,
                [
                    UUID(meta["actor_id"])
                    for meta in release_metadata.values()
                    if meta["actor_id"]
                ],
            )
            for meta in release_metadata.values():
                if meta["actor_id"]:
                    meta["actor_label"] = actor_names.get(
                        UUID(meta["actor_id"]), meta["actor_label"]
                    )

            health = [
                {
                    "revision_id": str(row[0]),
                    "scope": row[1],
                    "count": row[2],
                    "success_count": row[3],
                    "duration_ms": row[4],
                }
                for row in session.execute(
                    select(
                        ModelCall.revision_id,
                        ModelCall.scope,
                        func.count(),
                        func.sum(case((ModelCall.success.is_(True), 1), else_=0)),
                        func.sum(ModelCall.duration_ms),
                    )
                    .where(ModelCall.created_at > now() - timedelta(hours=24))
                    .group_by(ModelCall.revision_id, ModelCall.scope)
                )
            ]
            return {
                "enabled": settings.MODEL_CONFIG_ENABLED,
                "encryption_ready": bool(
                    settings.MODEL_CONFIG_ENCRYPTION_KEY.get_secret_value()
                ),
                "version": state.version if state else 0,
                "active_release": str(state.release_id)
                if state and state.release_id
                else None,
                "uses": USES,
                "enabled_uses": [
                    use
                    for use in USES
                    if settings.CLIPS_ENABLED
                    or use not in {"clip_analysis", "clip_timing"}
                ],
                "adapters": ADAPTERS,
                "resources": resources,
                "health": health,
                "probes": probes,
                "releases": [
                    {
                        "id": str(item.id),
                        "bindings": item.bindings,
                        "created_at": item.created_at,
                        **release_metadata.get(str(item.id), {}),
                    }
                    for item in releases
                ],
            }
