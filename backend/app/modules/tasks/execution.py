"""Owned Solo attempts: fence bootstrap errors and leave durable local recovery receipts."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import socket
from uuid import UUID, uuid4

from app.core.exceptions import AppException
from app.infrastructure.process_control import atomic_json, task_process_scope
from app.infrastructure.process_runner import ProcessRunner
from app.modules.tasks.service import database_call

TERMINAL = {"completed", "failed", "cancelled", "expired"}


@contextmanager
def execution_lock(storage_root, task_id):
    path = Path(storage_root) / ".locks" / ("execution-" + str(UUID(str(task_id))))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AppException("TASK_EXECUTION_BUSY", "该任务已有执行者", 409) from None
        yield
    finally:
        os.close(fd)


def receipt_path(storage_root, task_id, execution_id):
    return Path(storage_root) / ".attempts" / (str(UUID(str(task_id))) + "-" + str(UUID(execution_id)) + ".json")


def recoverable_receipt(storage_root, task):
    identifier = task.input_payload.get("_execution_id")
    if not identifier:
        return False
    try:
        data = json.loads(receipt_path(storage_root, task.id, identifier).read_text())
        return (data["execution_id"] == identifier and data["task_id"] == str(task.id)
                and data["host"] == task.worker_host == socket.gethostname()
                and data["worker_pid"] == task.worker_pid
                and data["state"] in {"claiming", "active", "stopped"})
    except (OSError, ValueError, KeyError, TypeError):
        return False


def discard_receipt(storage_root, task):
    identifier = task.input_payload.get("_execution_id")
    if identifier:
        receipt_path(storage_root, task.id, identifier).unlink(missing_ok=True)


async def cleanup_attempt_receipts(storage_root, tasks):
    """Reclaim closed terminal receipts even if the final DB reply was lost before tmp existed."""
    for index, path in enumerate((Path(storage_root) / ".attempts").glob("*.json")):
        if index >= 2048:
            break
        try:
            data = json.loads(path.read_text())
            task_id = UUID(data["task_id"])
            if data["host"] != socket.gethostname() or path != receipt_path(storage_root, task_id, data["execution_id"]):
                continue
            with execution_lock(storage_root, task_id):
                task = await database_call(tasks.get, task_id)
                if task.status in TERMINAL and ProcessRunner.directory_stopped(Path(storage_root) / "tmp" / str(task_id)):
                    path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        except AppException as error:
            if error.code not in {"TASK_EXECUTION_BUSY", "TASK_NOT_FOUND"}:
                raise


async def run_owned(task_id, service, body, *, start_options=None):
    """Hold an OS lock until all task code stops; a duplicate never settles someone else's attempt."""
    tasks, storage = service.tasks.repository, service.storage
    identifier = str(uuid4())
    path = receipt_path(storage.root, task_id, identifier)
    data = {"execution_id": identifier, "task_id": str(task_id), "host": socket.gethostname(),
            "worker_pid": os.getpid(), "state": "claiming"}
    result = None
    try:
        with execution_lock(storage.root, task_id):
            atomic_json(path, data)
            try:
                options = await start_options() if start_options else {}
                started = await database_call(tasks.start, task_id, execution_id=identifier, **options)
                if not started:
                    path.unlink(missing_ok=True)
                    task = await database_call(tasks.get, task_id)
                    return {"task_id": str(task_id), "status": task.status}
                data["state"] = "active"
                atomic_json(path, data)
                task = await database_call(tasks.get, task_id)
                with task_process_scope(storage.root / "tmp" / str(task_id), identifier, task.attempts):
                    result = await body(task)
                return result
            except Exception as error:
                code = error.code if isinstance(error, AppException) else "CLIP_PROCESSING_FAILED"
                message = error.message if isinstance(error, AppException) else "切片处理未完成，本次操作不扣额度"
                # Does not need the failed initial GET. PostgreSQL CAS verifies our exact execution token.
                task = await database_call(tasks.finish, task_id, "failed", 0, error_code=code,
                    error_message=message, expected_execution_id=identifier)
                result = {"task_id": str(task_id), "status": task.status}
                try:
                    await service.tasks.bus.publish(task_id, task.status, task.progress, error_code=task.error_code)
                except AppException:
                    pass
                return result
            finally:
                if path.exists():
                    data["state"] = "stopped"
                    try:
                        atomic_json(path, data)
                    except OSError:
                        pass  # Recovery also checks that this attempt's OS execution lock is no longer held.
                    if result and result["status"] in TERMINAL and ProcessRunner.directory_stopped(storage.root / "tmp" / str(task_id)):
                        path.unlink(missing_ok=True)
    except AppException as error:
        if error.code != "TASK_EXECUTION_BUSY":
            raise
        task = await database_call(tasks.get, task_id)
        return {"task_id": str(task_id), "status": task.status}
