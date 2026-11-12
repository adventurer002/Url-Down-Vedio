"""Real PostgreSQL bootstrap race and additive migration checks in disposable schemas."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from test_allowances_postgres import postgres
from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.admin.service import AdminService
from app.modules.audit.models import AuditEvent
from app.modules.auth.models import User
from app.modules.auth.repository import UserRepository
from app.modules.tasks.repository import TaskRepository


def test_postgres_only_one_first_admin_across_distinct_users(postgres):
    sessions, _, _, first, _ = postgres
    second = UserRepository(sessions).subscription("second-admin-candidate", True, login=True)
    barrier = Barrier(2)

    def initialize(user):
        barrier.wait(timeout=5)
        try:
            return AdminService(sessions).bootstrap(user.id, "operator", "first admin", str(user.id))
        except AppException as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(initialize, [first, second]))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert "ADMIN_ALREADY_INITIALIZED" in results
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(User).where(User.role == "admin")) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_postgres_same_bootstrap_key_is_one_audit(postgres):
    sessions, _, _, user, _ = postgres
    barrier = Barrier(3)

    def initialize(_):
        barrier.wait(timeout=5)
        return AdminService(sessions).bootstrap(user.id, "operator", "first admin", "same-bootstrap-key")

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(initialize, range(3)))
    assert len({result["audit_id"] for result in results}) == 1
    assert sum(not result["duplicate"] for result in results) == 1
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_postgres_concurrent_status_changes_and_grants_are_single_operations(postgres):
    sessions, _, _, target, _ = postgres
    users = UserRepository(sessions)
    admin = users.subscription("concurrent-admin", True, login=True)
    service = AdminService(sessions)
    service.bootstrap(admin.id, "operator", "fixture", "bootstrap")
    actor = users.get(admin.id)
    barrier = Barrier(3)

    def grant(_):
        barrier.wait(timeout=5)
        return service.grant_user(actor, target.id, "monthly", "fixture", "concurrent-grant", "req_pg")

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(grant, range(3)))
    assert sum(not item["duplicate"] for item in results) == 1
    assert len({item["audit_id"] for item in results}) == 1
    barrier = Barrier(2)

    def disable(index):
        barrier.wait(timeout=5)
        try:
            return service.change_user_status(actor, target.id, "disabled", "active", 0,
                                               "fixture", "disable-" + str(index), "req_pg")
        except AppException as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(disable, range(2)))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert "USER_STATUS_CONFLICT" in results
    assert users.get(target.id).auth_version == 1


def test_postgres_admission_refreshes_identity_after_waiting_for_account_lock(postgres):
    from threading import Event
    sessions, _, quotas, target, _ = postgres
    users = UserRepository(sessions)
    admin = users.subscription("admission-admin", True, login=True)
    service = AdminService(sessions)
    service.bootstrap(admin.id, "operator", "fixture", "bootstrap")
    quotas.balance(target.id)
    original = quotas.identities.billing_identity
    observed, release = Event(), Event()
    calls = 0

    def pause_after_first_read(session, user_id):
        nonlocal calls
        identity = original(session, user_id)
        calls += 1
        if calls == 1:
            observed.set()
            assert release.wait(5)
        return identity

    quotas.identities.billing_identity = pause_after_first_read
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(quotas.balance, target.id)
        try:
            assert observed.wait(5)
            service.change_user_status(users.get(admin.id), target.id, "disabled", "active", 0,
                                       "fixture", "disable", "req_pg")
        finally:
            release.set()
        with pytest.raises(AppException) as error:
            future.result(timeout=5)
        assert error.value.code == "ACCOUNT_NOT_BOUND"


def test_postgres_admin_cancel_completion_race_has_truthful_audit_and_one_settlement(postgres):
    sessions, tasks, quotas, user, create = postgres
    users = UserRepository(sessions)
    admin = users.subscription("cancel-race-admin", True, login=True)
    service = AdminService(sessions)
    service.bootstrap(admin.id, "operator", "fixture", "bootstrap")
    actor = users.get(admin.id)
    for index in range(5):
        task = create()
        assert tasks.start(task.id)
        barrier = Barrier(2)

        def complete():
            barrier.wait(timeout=5)
            return tasks.complete_with(task.id, {"result_id": str(uuid4())}, lambda session: None)

        def cancel():
            barrier.wait(timeout=5)
            return service.cancel_task(actor, task.id, "fixture", "race-" + str(index), "req_pg")

        with ThreadPoolExecutor(max_workers=2) as pool:
            completed, cancelled = pool.submit(complete), pool.submit(cancel)
            completed.result(timeout=10)
            response = cancelled.result(timeout=10)
        final = tasks.get(task.id)
        if final.cancel_requested and final.status != "cancelled":
            final = tasks.finish(task.id, "cancelled", 0)
        assert final.status in {"completed", "cancelled"}
        with sessions() as session:
            audit = session.scalar(select(AuditEvent).where(AuditEvent.idempotency_key == "race-" + str(index)))
            if response["status"] == "completed":
                assert audit.outcome == "unchanged" and not response["cancel_requested"]
            else:
                assert response["cancel_requested"] and audit.outcome in {"accepted", "succeeded"}
        assert quotas.balance(user.id)["balances"]["ai_generations"]["held"] == 0


def test_postgres_0008_migration_preserves_users_and_matches_models():
    base = create_engine(settings.DATABASE_URL, connect_args={"connect_timeout": 1})
    schema = "test_admin_migration_" + uuid4().hex
    try:
        with base.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    except Exception:
        base.dispose()
        pytest.skip("PostgreSQL unavailable; migration QA requires a local test-capable database")
    scoped = create_engine(settings.DATABASE_URL, connect_args={"connect_timeout": 1,
        "options": f"-c search_path={schema} -c statement_timeout=5000"})
    try:
        config = Config("alembic.ini")
        # env.py still runs real migrations/metadata comparison, with its engine
        # construction redirected only to this isolated schema, never public.
        with patch("sqlalchemy.create_engine", return_value=scoped):
            command.upgrade(config, "0007")
            assert "audit_events" not in inspect(scoped).get_table_names()
            sessions = sessionmaker(bind=scoped, expire_on_commit=False)
            user_id = uuid4()
            visible_task, removed_task = uuid4(), uuid4()
            # Seed the old schema without selecting columns introduced after 0007.
            with scoped.begin() as connection:
                connection.execute(text("INSERT INTO users (id,wechat_openid,nickname,role,status,last_login_at) "
                    "VALUES (:id,'migration-existing-user','fixture','user','active',now())"), {"id": user_id})
                connection.execute(text(
                    "INSERT INTO tasks (id,user_id,task_type,history_hidden) "
                    "VALUES (:id,:user_id,'simulate_progress',:hidden)"
                ), [{"id": visible_task, "user_id": user_id, "hidden": False},
                    {"id": removed_task, "user_id": user_id, "hidden": True}])
            command.upgrade(config, "head")
            command.check(config)
            with scoped.connect() as connection:
                assert connection.execute(text("SELECT task_id FROM task_history_entries")).scalars().all() == [visible_task]
            assert TaskRepository(sessions).delete_history(user_id, visible_task) == 1
            result = AdminService(sessions).bootstrap(user_id, "operator", "migration QA", "migration-key")
            assert not result["duplicate"]
            command.downgrade(config, "0007")
            assert "audit_events" not in inspect(scoped).get_table_names()
            with scoped.connect() as connection:
                assert connection.scalar(text("SELECT role FROM users WHERE id=:id"), {"id": user_id}) == "admin"
            command.upgrade(config, "head")
            command.check(config)
            with scoped.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM task_history_entries")) == 0
    finally:
        scoped.dispose()
        with base.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        base.dispose()
