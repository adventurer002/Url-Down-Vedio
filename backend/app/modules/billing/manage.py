"""Audited operator grants and bounded reconciliation; never proof of payment."""
import argparse
import asyncio
from datetime import timedelta
import json
import os
import socket
from uuid import UUID

from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.process_runner import ProcessRunner
from app.modules.billing.models import AllowanceTransfer
from app.modules.billing.policy import PRODUCTS, now
from app.modules.billing.repository import AllowanceRepository
from app.modules.billing.service import resume_queued
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import database_call


def recover(tasks, storage_path):
    restored = 0
    from app.infrastructure.storage.local import LocalStorageAdapter
    storage = LocalStorageAdapter(storage_path, settings.STORAGE_MAX_QUOTA_MB, settings.STORAGE_MIN_FREE_MB)
    from app.modules.tasks.execution import execution_lock, recoverable_receipt, discard_receipt
    for task in tasks.running():
        if ("allowance" not in task.input_payload and task.task_type not in {"media_probe","model_test"}) or not task.worker_pid or task.worker_host != socket.gethostname():
            continue
        try:
            with execution_lock(storage_path, task.id):
                task = tasks.get(task.id)
                if task.status not in {"running", "processing"}:
                    continue
                closed_attempt = recoverable_receipt(storage_path, task)
                if not closed_attempt:
                    try:
                        os.kill(task.worker_pid, 0)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        continue
                    else:
                        continue  # Live legacy workers without an execution receipt remain protected.
                if not ProcessRunner.directory_stopped(storage_path / "tmp" / str(task.id)):
                    continue
                final = tasks.finish(task.id, "failed" if closed_attempt else "cancelled", task.progress,
                    error_code="TASK_EXECUTION_INTERRUPTED" if closed_attempt else "WORKER_LOST",
                    error_message="执行已停止且进程组已退出，已释放额度",
                    expected_execution_id=task.input_payload.get("_execution_id"))
                if final.status not in {"failed", "cancelled", "expired"}:
                    continue  # Completion or a different execution won; never delete its artifacts.
                from app.modules.media.repository import MediaRepository
                storage.discard_batch(task.id, MediaRepository(tasks.sessions).storage_keys())
                storage.remove_task_directory(task.id)
                storage.release_reservation(task.id)
                discard_receipt(storage_path, task)
                if task.task_type == "media_probe":
                    from app.modules.uploads.repository import UploadRepository
                    UploadRepository(tasks.sessions).terminal(UUID(task.input_payload["upload_id"]), task.user_id,
                        "failed", "WORKER_LOST")
                restored += 1
        except AppException as error:
            if error.code != "TASK_EXECUTION_BUSY":
                raise
    return restored


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    grant = sub.add_parser("grant")
    grant.add_argument("--user-id", type=UUID, required=True)
    grant.add_argument("--product", choices=PRODUCTS, required=True)
    grant.add_argument("--key", required=True)
    grant.add_argument("--actor", required=True)
    grant.add_argument("--reason", required=True)
    sub.add_parser("reconcile")
    args = parser.parse_args(argv)
    repository, tasks = AllowanceRepository(SessionLocal), TaskRepository(SessionLocal)
    if args.command == "grant":
        from app.modules.admin.service import AdminService
        from uuid import uuid4

        async def perform():
            service = AdminService(SessionLocal)
            result = await database_call(service.grant_user, None, args.user_id, args.product,
                args.reason, args.key, "cli_" + uuid4().hex, operator=args.actor)
            return await service.wake_after_grant(args.user_id, result)

        try:
            result = asyncio.run(perform())
        except AppException as error:
            print(json.dumps({"error": {"code": error.code, "message": error.message}}, ensure_ascii=False))
            return 1
        except Exception:
            print(json.dumps({"error": {"code": "ADMIN_OPERATION_FAILED", "message": "发放失败，请检查服务配置"}}, ensure_ascii=False))
            return 1
    else:
        result = {"recovered": recover(tasks, settings.storage_path)}
        with SessionLocal() as session:
            # A crashed HTTP transfer cannot report its last bytes. Conservatively
            # account for its entire reservation; do not enable unlimited reconnects.
            stale = [(x.id, x.reserved) for x in session.scalars(select(AllowanceTransfer).where(
                AllowanceTransfer.sent.is_(None), AllowanceTransfer.created_at < now() - timedelta(hours=1)).limit(100))]
        for identifier, reserved in stale:
            repository.finish_transfer(identifier, reserved)
        result.update(transfers_reconciled=len(stale), dispatched=asyncio.run(resume_queued(tasks)))
    print(json.dumps(result, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
