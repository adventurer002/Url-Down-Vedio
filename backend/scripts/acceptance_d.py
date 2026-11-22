"""Run bounded multi-account capacity waves against an isolated VideoHub API."""
import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx

POPULATIONS = {1, 2, 5, 10}
ACTIONS = {"download", "transcription", "ai_summary", "ai_mindmap"}
TERMINAL = {"completed", "failed", "cancelled", "expired"}
WORK_BY_ACTION = {
    "download": {"source_extract", "source_transfer", "download_render"},
    "transcription": {"subtitles", "audio_acquire", "asr"},
    "ai_summary": {"subtitles", "audio_acquire", "asr", "summary"},
    "ai_mindmap": {"subtitles", "audio_acquire", "asr", "summary", "mindmap"},
}
LABEL = re.compile(r"[A-Za-z0-9_-]{1,48}")
STAGE_LOG = re.compile(
    r"performance stage=(\w+) task_id=([a-f0-9-]{36}) duration_ms=(\d+) outcome=(\w+) "
    r"started_unix_ms=(\d+) pid=(\d+)"
)
QUEUE_LOG = re.compile(r"performance phase=(\w+) task_id=([a-f0-9-]{36}) dispatch_to_claim_ms=(\d+)")


class ProbeFailure(Exception):
    pass


def validate_manifest(waves):
    if not isinstance(waves, list) or not 4 <= len(waves) <= 100:
        raise ValueError
    populations, ids = set(), set()
    for wave in waves:
        jobs = wave["jobs"]
        users = {job["account"] for job in jobs}
        population = wave["users"]
        if population not in POPULATIONS or len(users) != population or not population <= len(jobs) <= 30:
            raise ValueError
        populations.add(population)
        for job in jobs:
            if not LABEL.fullmatch(job["id"]) or job["id"] in ids or not LABEL.fullmatch(job["account"]):
                raise ValueError
            ids.add(job["id"])
            quote = job["quote"]
            if quote["action"] not in ACTIONS or len({"url", "upload_id", "transcript_id"} & quote.keys()) != 1:
                raise ValueError
            if job["cache"] not in {"cold", "hit"}:
                raise ValueError
            delivery = job.get("delivery")
            if delivery is not None and (quote["action"] != "download" or delivery not in {"local", "gateway", "direct"}):
                raise ValueError
    if populations != POPULATIONS:
        raise ValueError
    return waves


def epoch_ms():
    return time.time_ns() // 1_000_000


def elapsed(start):
    return round((time.monotonic() - start) * 1000, 2)


def server_ms(value):
    date = datetime.fromisoformat(value)
    return round(date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp() * 1000)


def origin(url):
    parts = urlsplit(url)
    if not parts.hostname or parts.username or parts.password or parts.scheme not in {"http", "https"}:
        raise ValueError
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


async def request_data(client, method, path, token, **kwargs):
    response = await client.request(method, path, headers={"Authorization": "Bearer " + token}, **kwargs)
    if not response.is_success:
        try:
            code = response.json()["error"]["code"]
        except (ValueError, TypeError, KeyError):
            code = None
        raise ProbeFailure(code if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,100}", code)
                           else f"HTTP_{response.status_code}")
    return response.json()["data"]


class WorkerLogs:
    """Read only new lines, and keep only structured timing fields."""
    def __init__(self, paths):
        self.logs = []
        self.work = defaultdict(list)
        self.queue = defaultdict(list)
        for path in paths:
            self.logs.append((path, path.stat().st_size))

    def refresh(self):
        for index, (path, offset) in enumerate(self.logs):
            with path.open("rb") as log:
                log.seek(offset)
                while line := log.readline():
                    if not line.endswith(b"\n"):
                        break
                    offset = log.tell()
                    line = line.decode(errors="replace")
                    if match := STAGE_LOG.search(line):
                        stage, task_id, duration, outcome, started, pid = match.groups()
                        self.work[task_id].append({"stage": stage, "started_unix_ms": int(started),
                            "duration_ms": int(duration), "outcome": outcome, "pid": int(pid)})
                    elif match := QUEUE_LOG.search(line):
                        phase, task_id, delay = match.groups()
                        self.queue[task_id].append({"phase": phase, "dispatch_to_claim_ms": int(delay)})
                self.logs[index] = (path, offset)


async def download(client, token, result, mode, object_origin, row):
    file_id = UUID(result["file_id"])
    expected = result["size_bytes"]
    start = time.monotonic()
    url = f"/files/{file_id}/download"
    if mode == "local":
        url += "?delivery=local"
    headers = {"Authorization": "Bearer " + token}
    async with client.stream("GET", url, headers=headers) as response:
        if mode == "local":
            response.raise_for_status()
            size = await count_bytes(response)
        else:
            target = response.headers.get("location")
            if response.status_code != 307 or not target or origin(target) != object_origin:
                raise ProbeFailure("OBJECT_DELIVERY_UNAVAILABLE")
    if mode != "local":
        async with client.stream("GET", target) as response:
            response.raise_for_status()
            size = await count_bytes(response)
    row.update(delivery=mode, delivery_ms=elapsed(start), delivery_bytes=size)
    if size != expected:
        raise ProbeFailure("DELIVERY_SIZE_MISMATCH")


async def count_bytes(response):
    size = 0
    async for chunk in response.aiter_raw():
        size += len(chunk)
    return size


async def run_job(client, job, token, timeout, poll_interval, logs, object_origin):
    quote = job["quote"]
    row = {"id": job["id"], "account": job["account"], "action": quote["action"], "expected_cache": job["cache"]}
    submitted = time.monotonic()
    step = "quote"
    try:
        async with asyncio.timeout(timeout):
            offered = await request_data(client, "POST", "/billing/quotes", token, json=quote)
            row["quote_ms"] = elapsed(submitted)
            step = "confirm"
            start = time.monotonic()
            accepted = await request_data(client, "POST", "/billing/tasks", token, json={"quote_id": offered["quote_id"]})
            task_id = str(UUID(accepted["task_id"]))
            row.update(task_id=task_id, admission_ms=elapsed(start), quote_and_admission_ms=elapsed(submitted))
            step = "task"
            while True:
                task = await request_data(client, "GET", f"/tasks/{task_id}", token)
                status = task["status"]
                row["last_status"] = status
                if task.get("wait_reason"):
                    row["last_wait_reason"] = task["wait_reason"]
                if status in TERMINAL:
                    break
                await asyncio.sleep(poll_interval)
            row.update(status=status, task_observed_ms=elapsed(submitted), created_unix_ms=server_ms(task["created_at"]),
                       task_error_code=task["error_code"])
            if task["finished_at"]:
                row["task_server_ms"] = server_ms(task["finished_at"]) - row["created_unix_ms"]
            if status == "completed":
                result = task["result_payload"]
                row["result_cache_hit"] = bool(result.get("cache_hit") or result.get("reused"))
                if delivery := job.get("delivery"):
                    step = "delivery"
                    if delivery != "local" and not row["result_cache_hit"]:
                        while True:
                            logs.refresh()
                            if any(entry["stage"] == "object_upload" and entry["outcome"] == "ok"
                                   for entry in logs.work[task_id]):
                                break
                            await asyncio.sleep(poll_interval)
                        await asyncio.sleep(poll_interval)
                    await download(client, token, result, delivery, object_origin, row)
    except TimeoutError:
        row["probe_error"] = "DEADLINE"
    except ProbeFailure as error:
        row["probe_error"] = str(error)
    except httpx.HTTPStatusError as error:
        row["probe_error"] = f"HTTP_{error.response.status_code}"
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        row["probe_error"] = type(error).__name__
    if "probe_error" in row:
        row["probe_error_step"] = step
        row["possibly_in_flight"] = (step == "task" or (step == "confirm" and
            ("task_id" in row or row["probe_error"] not in {"HTTP_401", "HTTP_403", "HTTP_422"})))
    return row


def percentile(values, quantile=.95):
    if not values:
        return None
    return sorted(values)[math.ceil(len(values) * quantile) - 1]


def add_evidence(rows, logs):
    logs.refresh()
    for row in rows:
        if not (task_id := row.get("task_id")):
            continue
        row["work"] = sorted(logs.work[task_id], key=lambda stage: stage["started_unix_ms"])
        row["dispatch_to_claim"] = logs.queue[task_id]
        if row["action"] == "download":
            row["source_metadata_attempts"] = [entry["stage"].removeprefix("source_") for entry in row["work"]
                                               if entry["stage"] in {"source_extract", "source_cached"}]
        meaningful = [entry for entry in row["work"] if entry["stage"] in WORK_BY_ACTION[row["action"]]]
        if meaningful and not row.get("result_cache_hit") and "created_unix_ms" in row:
            delay = meaningful[0]["started_unix_ms"] - row["created_unix_ms"]
            if delay >= 0:
                row["first_work_wait_ms"] = delay


def summarize(rows, wave_seconds):
    admission = [row["admission_ms"] for row in rows if "admission_ms" in row]
    cold = [row for row in rows if row["expected_cache"] == "cold"]
    work = [row["first_work_wait_ms"] for row in cold if "first_work_wait_ms" in row]
    completed = sum(row.get("status") == "completed" and "probe_error" not in row for row in rows)
    matched = sum(row.get("result_cache_hit") == (row["expected_cache"] == "hit") for row in rows)
    phases = defaultdict(list)
    for row in rows:
        for phase in row.get("dispatch_to_claim", []):
            phases[phase["phase"]].append(phase["dispatch_to_claim_ms"])
    return {
        "submitted": len(rows), "admitted": len(admission), "completed_with_checks": completed,
        "cache_expectations_matched": matched, "cold_expected": len(cold), "cold_work_observed": len(work),
        "quote_p95_ms": percentile([row["quote_ms"] for row in rows if "quote_ms" in row]),
        "quote_and_admission_p95_ms": percentile([row["quote_and_admission_ms"] for row in rows if "quote_and_admission_ms" in row]),
        "admission_p95_ms": percentile(admission), "first_work_p95_ms": percentile(work),
        "dispatch_to_claim_by_phase": {phase: {"count": len(values), "p95_ms": percentile(values)}
                                       for phase, values in sorted(phases.items())},
        "completed_per_minute": round(completed * 60 / wave_seconds, 2),
        "targets_met": (completed == matched == len(rows) and len(admission) == len(rows)
                        and 0 < len(work) == len(cold) and percentile(admission) <= 1000
                        and percentile(work) <= 2000),
    }


class LinuxSampler:
    def __init__(self, interface, storage):
        self.network = Path("/sys/class/net") / interface / "statistics"
        self.storage = storage
        self.previous = None
        self.samples = []

    def sample(self):
        with Path("/proc/stat").open() as file:
            ticks = [int(value) for value in file.readline().split()[1:9]]
        mem = {fields[0]: int(fields[1]) for line in Path("/proc/meminfo").read_text().splitlines()
               if (fields := line.split())[0] in {"MemAvailable:", "MemTotal:", "SwapFree:", "SwapTotal:"}}
        rx, tx = (int((self.network / name).read_text()) for name in ("rx_bytes", "tx_bytes"))
        now, total, idle = time.monotonic(), sum(ticks), ticks[3] + ticks[4]
        current = (now, total, idle, rx, tx, ticks[4], ticks[7])
        if self.previous:
            old_time, old_total, old_idle, old_rx, old_tx, old_iowait, old_steal = self.previous
            interval = now - old_time
            cpu_ticks = max(total - old_total, 1)
            self.samples.append({
                "at_unix_ms": epoch_ms(), "interval_seconds": round(interval, 3),
                "cpu_busy_pct": round(100 * (1 - (idle - old_idle) / (total - old_total)), 2) if total > old_total else None,
                "cpu_iowait_pct": round(100 * max(0, ticks[4] - old_iowait) / cpu_ticks, 2),
                "cpu_steal_pct": round(100 * max(0, ticks[7] - old_steal) / cpu_ticks, 2),
                "memory_total_kib": mem["MemTotal:"], "memory_available_kib": mem["MemAvailable:"],
                "swap_used_kib": mem["SwapTotal:"] - mem["SwapFree:"],
                "rx_mbps": round(max(0, rx - old_rx) * 8 / interval / 1e6, 3),
                "tx_mbps": round(max(0, tx - old_tx) * 8 / interval / 1e6, 3),
                "storage_free_bytes": shutil.disk_usage(self.storage).free,
            })
        self.previous = current

    async def run(self, interval):
        while True:
            await asyncio.sleep(interval)
            self.sample()


async def run_wave(client, wave, tokens, *, timeout, poll_interval, logs, object_origin):
    start = time.monotonic()
    started_at = epoch_ms()
    rows = await asyncio.gather(*(run_job(client, job, tokens[job["account"]], timeout, poll_interval, logs, object_origin)
                                  for job in wave["jobs"]))
    # A status can become visible immediately before the worker flushes its last timer.
    await asyncio.sleep(poll_interval)
    add_evidence(rows, logs)
    seconds = time.monotonic() - start
    return {"users": wave["users"], "started_unix_ms": started_at, "finished_unix_ms": epoch_ms(),
            "seconds": round(seconds, 3), "jobs": rows, "metrics": summarize(rows, seconds)}


def save_report(path, report):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


async def run_campaign(args, waves):
    tokens = json.loads(args.tokens.read_text())
    labels = {job["account"] for wave in waves for job in wave["jobs"]}
    logs = WorkerLogs(args.worker_log)
    object_origin = origin(args.object_origin) if args.object_origin else None
    if not object_origin and any(job.get("delivery") in {"direct", "gateway"} for wave in waves for job in wave["jobs"]):
        raise ValueError
    sampler = LinuxSampler(args.interface, args.storage) if args.interface else None
    if sampler:
        sampler.sample()
    sampling = asyncio.create_task(sampler.run(args.sample_interval)) if sampler else None
    report = {"environment": args.environment, "logical_cpus": os.cpu_count(),
              "sampling_interface": args.interface, "started_unix_ms": epoch_ms(), "waves": [], "complete": False}
    try:
        async with httpx.AsyncClient(base_url=args.api.rstrip("/") + "/api/v1", trust_env=False,
                                     timeout=httpx.Timeout(90, connect=5),
                                     limits=httpx.Limits(max_connections=30)) as client:
            identities = set()
            for label in sorted(labels):
                async with asyncio.timeout(10):
                    user = await request_data(client, "GET", "/auth/me", tokens[label])
                identities.add(user["id"])
            if len(identities) != len(labels):
                raise ProbeFailure("ACCOUNTS_NOT_DISTINCT")
            await asyncio.sleep(args.baseline_seconds)
            for wave in waves:
                item = await run_wave(client, wave, tokens, timeout=args.timeout, poll_interval=args.poll_interval,
                                      logs=logs, object_origin=object_origin)
                report["waves"].append(item)
                report["host_samples"] = sampler.samples if sampler else []
                save_report(args.output, report)
                if any(job.get("possibly_in_flight") for job in item["jobs"]):
                    raise ProbeFailure("CHECK_FOR_IN_FLIGHT_TASKS_BEFORE_NEXT_WAVE")
            report["complete"] = True
    finally:
        if sampling:
            sampling.cancel()
            try:
                await sampling
            except asyncio.CancelledError:
                pass
            except OSError:
                report["sampling_error"] = "HOST_SAMPLING_FAILED"
        report["host_samples"] = sampler.samples if sampler else []
        report["finished_unix_ms"] = epoch_ms()
        save_report(args.output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--api", help="Origin of a dedicated loopback API")
    parser.add_argument("--environment", choices=("synthetic", "isolated-cloud"))
    parser.add_argument("--tokens", type=Path, help="Private JSON mapping account labels to access tokens")
    parser.add_argument("--worker-log", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--object-origin", "--gateway", help="Expected COS or object gateway origin")
    parser.add_argument("--interface", help="Linux network interface for host sampling")
    parser.add_argument("--storage", type=Path, help="Path on the task storage filesystem")
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--poll-interval", type=float, default=1)
    parser.add_argument("--sample-interval", type=float, default=1)
    parser.add_argument("--baseline-seconds", type=float, default=15)
    args = parser.parse_args()
    try:
        waves = validate_manifest(json.loads(args.manifest.read_text()))
        if args.validate:
            print(json.dumps({"valid": True, "waves": [wave["users"] for wave in waves]}))
            return
        if (not args.api or not args.tokens or not args.output or not args.environment or not args.worker_log
                or origin(args.api)[1] not in {"127.0.0.1", "::1"}
                or (args.environment == "isolated-cloud" and (not args.interface or not args.storage))
                or (args.interface and (not args.storage or not re.fullmatch(r"[\w.-]+", args.interface)))
                or not 0 < args.timeout <= 7200 or not .1 <= args.poll_interval <= 5
                or not .5 <= args.sample_interval <= 10 or not 0 <= args.baseline_seconds <= 600):
            parser.error("Expected isolated loopback API, account file, worker logs, output and valid timing options")
        asyncio.run(run_campaign(args, waves))
    except (OSError, ValueError, KeyError, TypeError, TimeoutError, ProbeFailure, httpx.HTTPError) as error:
        print(str(error) if isinstance(error, ProbeFailure) else type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
