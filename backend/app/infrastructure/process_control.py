"""Task-scoped native process receipts, including legacy nested watchdog markers."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import socket
import stat
from typing import Protocol
from uuid import UUID, uuid4

from app.core.exceptions import AppException

_owner = ContextVar("native_process_owner", default=None)
MAX_DIRECTORIES = 512
MAX_RECORDS = 2048


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".part")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def task_process_scope(root, execution_id, attempts):
    token = _owner.set({"root": Path(root), "execution_id": execution_id, "attempts": attempts})
    try:
        yield
    finally:
        _owner.reset(token)


def task_root(directory: Path) -> Path:
    directory = Path(directory)
    scope = _owner.get()
    if scope and (directory == scope["root"] or scope["root"] in directory.parents):
        return scope["root"]
    for item in (directory, *directory.parents):
        if item.parent.name == "tmp":
            try:
                UUID(item.name)
                return item
            except ValueError:
                pass
    return directory


class ProcessControl(Protocol):
    def prepare(self, directory: Path) -> tuple[Path, dict]: ...
    def started(self, receipt: tuple[Path, dict], pgid: int, directory: Path): ...
    def exited(self, receipt: tuple[Path, dict]): ...
    def all_stopped(self, directory: Path, group_exists) -> bool: ...


class FileProcessControl:
    def prepare(self, directory):
        root = task_root(directory)
        if root.is_symlink():
            raise AppException("PROCESS_CONTROL_INVALID", "任务进程登记目录无效", 500)
        scope = _owner.get() or {}
        identifier = uuid4().hex
        path = root / ".operations" / (identifier + ".json")
        data = {"operation_id": identifier, "task_id": root.name, "host": socket.gethostname(),
                "worker_pid": os.getpid(), "work_directory": str(Path(directory).relative_to(root)),
                "execution_id": scope.get("execution_id"), "attempts": scope.get("attempts"),
                "state": "starting", "pgid": None}
        atomic_json(path, data)  # Unknown spawn outcomes must remain protected, even before the PID is known.
        return path, data

    def started(self, receipt, pgid, directory):
        path, data = receipt
        data.update(state="running", pgid=pgid)
        atomic_json(path, data)
        atomic_json(Path(directory) / ".process.json", {"pgid": pgid, "operation_id": data["operation_id"]})

    def exited(self, receipt):
        path, data = receipt
        data["state"] = "exited"
        atomic_json(path, data)

    def all_stopped(self, directory, group_exists):
        root = task_root(directory)
        try:
            info = root.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if not stat.S_ISDIR(info.st_mode):
            return False
        receipts, legacy = {}, []
        visited = records = 0
        try:
            def scan_error(error):
                raise error
            for folder, dirs, files in os.walk(root, followlinks=False, onerror=scan_error):
                visited += 1
                if visited > MAX_DIRECTORIES or any((Path(folder) / name).is_symlink() for name in dirs):
                    return False
                for name in files:
                    path = Path(folder) / name
                    if name != ".process.json" and not (path.parent.name == ".operations" and path.suffix == ".json"):
                        continue
                    records += 1
                    if records > MAX_RECORDS or path.is_symlink():
                        return False
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        continue  # A safely removed old subdirectory is no longer a reader.
                    if name == ".process.json":
                        legacy.append(data)
                    else:
                        if data["operation_id"] != path.stem or data["task_id"] != root.name:
                            return False
                        receipts[data["operation_id"]] = data
            for data in receipts.values():
                if data["state"] == "exited":
                    continue
                if data["state"] != "running" or data["host"] != socket.gethostname():
                    return False
                pgid = data["pgid"]
                if type(pgid) is not int or not 1 < pgid <= 2147483647 or group_exists(pgid):
                    return False
            for data in legacy:
                receipt = receipts.get(data.get("operation_id"))
                if receipt and receipt["state"] == "exited":
                    if receipt["pgid"] != data["pgid"]:
                        return False
                    continue  # Do not mistake a recycled old PGID for our confirmed exited operation.
                pgid = data["pgid"]
                if type(pgid) is not int or not 1 < pgid <= 2147483647 or group_exists(pgid):
                    return False
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False
        return True


@contextmanager
def guarded_temporary_directory(*, prefix, dir):
    """Unlike TemporaryDirectory, preserve data when native exit is unconfirmed."""
    import shutil
    import tempfile
    from app.infrastructure.process_runner import ProcessRunner
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=dir))
    preserve = False
    try:
        yield str(directory)
    except BaseException as error:
        preserve = isinstance(error, AppException) and error.code == "PROCESS_CLEANUP_FAILED"
        raise
    finally:
        if not preserve and ProcessRunner.directory_stopped(directory):
            shutil.rmtree(directory, ignore_errors=False) if directory.exists() else None
