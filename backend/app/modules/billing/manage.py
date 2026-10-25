"""Audited operator grants and bounded reconciliation; never proof of payment."""
import argparse
import asyncio
from datetime import timedelta
import json
import os
import socket
import shutil
from uuid import UUID

from sqlalchemy import select

from app.core.config import settings
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.process_runner import ProcessRunner
from app.modules.billing.models import AllowanceTransfer
from app.modules.billing.policy import PRODUCTS, now
from app.modules.billing.repository import AllowanceRepository
from app.modules.billing.service import resume_queued
from app.modules.tasks.repository import TaskRepository


def recover(tasks, storage_path):
    restored = 0
    for task in tasks.running():
        if "allowance" not in task.input_payload or not task.worker_pid or task.worker_host != socket.gethostname():
            continue
        try:
            os.kill(task.worker_pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            continue
        else:
            continue  # A live worker must finish via its watchdog; never release early.
        marker = storage_path / "tmp" / str(task.id) / ".process.json"
        if marker.exists():
            try:
                pgid = json.loads(marker.read_text())["pgid"]
                if type(pgid) is not int or pgid <= 1 or ProcessRunner.group_exists(pgid):
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                continue
        tasks.request_cancel(task.id)
        directory = storage_path / "tmp" / str(task.id)
        if directory.exists():
            shutil.rmtree(directory)
        tasks.finish(task.id, "cancelled", task.progress, error_code="WORKER_LOST", error_message="Worker 中断，已释放额度")
        restored += 1
    return restored


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    grant = sub.add_parser("grant")
    grant.add_argument("--user-id", type=UUID, required=True)
    grant.add_argument("--product", choices=PRODUCTS, required=True)
    grant.add_argument("--key", required=True)
    grant.add_argument("--actor", required=True)
    sub.add_parser("reconcile")
    args = parser.parse_args()
    repository, tasks = AllowanceRepository(SessionLocal), TaskRepository(SessionLocal)
    if args.command == "grant":
        result = repository.grant(args.user_id, args.product, args.key, args.actor)
        result["dispatched"] = asyncio.run(resume_queued(tasks, args.user_id))
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


if __name__ == "__main__":
    main()
