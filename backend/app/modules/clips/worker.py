"""Celery Solo highlight pipeline; no HTTP processing or child-task waits."""
import asyncio
from dataclasses import asdict
from contextlib import ExitStack
from datetime import timedelta
from uuid import UUID, uuid4

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.celery_app import celery_app
from app.infrastructure.queue.progress_bus import ProgressBus
from app.infrastructure.storage.retry import wait_for_storage
from app.infrastructure.timing import log_queue_wait, stage_timer
from app.modules.ai.providers import get_llm_provider
from app.modules.ai.providers.bounded import BoundedLLMProvider
from app.modules.clips.media import FFmpegClipAdapter
from app.modules.clips.cache import candidate_key, window_key, asset_key, TIMING_VERSION
from app.modules.clips.selection import LLMClipSelector, plan_key, timeline_hash
from app.modules.clips.service import clip_service, enabled, selected_candidates
from app.modules.clips.timing import locate, timing_provider, transcribe_window, playback_ranges
from app.modules.media.repository import MediaRecord
from app.modules.media.service import safe_filename
from app.modules.tasks.repository import TaskRepository, utcnow
from app.modules.tasks.service import TaskService, database_call
from app.modules.tasks.execution import run_owned
from app.modules.transcript.service import TranscriptionPipeline


class Progress:
    def __init__(self, tasks, task_id):
        self.tasks, self.task_id, self.value = tasks, task_id, 0

    async def __call__(self, stage, value):
        self.value = max(self.value, min(99, value))
        await database_call(self.tasks.repository.mark_processing, self.task_id)
        try:
            await self.tasks.bus.publish(self.task_id, "processing", self.value, stage=stage)
        except AppException:
            pass


async def terminal(service, task_id, error=None):
    current = await database_call(service.tasks.repository.get, task_id)
    if error is not None and current.status != "completed":
        code = error.code if isinstance(error, AppException) else "CLIP_TIMEOUT" if isinstance(error, TimeoutError) else "CLIP_PROCESSING_FAILED"
        message = error.message if isinstance(error, AppException) else "切片处理未完成，本次操作不扣额度"
        current = await database_call(service.tasks.repository.finish, task_id,
            "cancelled" if code == "TASK_CANCELLED" else "failed", current.progress, error_code=code, error_message=message)
    try:
        await service.tasks.bus.publish(task_id, current.status, current.progress, error_code=current.error_code)
    except AppException:
        pass
    return {"task_id": str(task_id), "status": current.status}


async def execute_plan(task_id, service, provider=None, pipeline=None):
    return await run_owned(task_id, service, lambda task: _execute_plan(task, service, provider, pipeline))


async def _execute_plan(task, service, provider=None, pipeline=None):
    task_id = task.id
    tasks = service.tasks.repository
    log_queue_wait(task)
    payload = task.input_payload
    progress = Progress(service.tasks, task_id)
    try:
        enabled()
        async with asyncio.timeout(payload["allowance"]["clip_limits"]["analysis_timeout"]):
            await service.revalidate(task.user_id, task.task_type, payload)
            await progress("acquiring_transcript", 5)
            pipeline = pipeline or TranscriptionPipeline(service.tasks, service.repository.sessions, service.storage)

            async def transcript_progress(event):
                await progress("acquiring_transcript", 5 + int(event.get("progress", 0) / 5))

            transcript = await pipeline.acquire(task_id, on_progress=transcript_progress)
            spec = payload["clip_spec"]
            key = plan_key(task.user_id, transcript, spec)
            cached = await database_call(service.repository.cached_plan, task.user_id, key)
            if payload.get("reuse_result") and (cached is None or str(cached["id"]) != payload["reuse_result"]["clip_plan_id"]):
                raise AppException("CLIP_REQUOTE_REQUIRED", "已报价的方案缓存失效，请重新报价", 409)
            if cached:
                result = {"clip_plan_id": str(cached["id"]), "transcript_id": str(transcript.id),
                          "outcome": cached["outcome"], "cache_hit": True}
                await database_call(tasks.complete_with, task_id, result, lambda session: None)
            else:
                if payload.get("model_config"):
                    from app.modules.model_config.runtime import llm
                    provider=await database_call(llm,"clip_analysis",payload["model_config"],tasks.sessions)
                else:provider = provider or (get_llm_provider(managed=False) if settings.MODEL_CONFIG_ENABLED else get_llm_provider())
                if provider.name == "openai_compatible":
                    provider.model = spec["model"]
                elif provider.model != spec["model"]:
                    raise AppException("CLIP_REQUOTE_REQUIRED", "模型配置已改变，请重新报价", 409)
                if settings.APP_ENV != "development" and provider.name == "heuristic":
                    raise AppException("LLM_NOT_CONFIGURED", "生产切片分析需要真实模型", 503)
                bounded = BoundedLLMProvider(provider, payload["allowance"]["ai_limits"])
                with stage_timer("clip_selection", task_id):
                    candidates, usage, reason = await LLMClipSelector(bounded).select(transcript, spec,
                        lambda: service.tasks.is_cancel_requested(task_id), progress)
                source = await database_call(service.videos.clip_source, task.video_id, task.user_id)
                content = {"spec": spec, "candidates": candidates, "outcome": "highlights" if candidates else "no_highlights",
                    "reason": reason, "usage": asdict(usage), "source_basis": transcript.source_type,
                    "timeline_hash": timeline_hash(transcript), "transcript_content_hash": transcript.content_hash,
                    "source": source, "mock": provider.name == "heuristic"}

                def store(session):
                    identifier = service.repository.store_plan(session, task, transcript, key, content)
                    return {"clip_plan_id": str(identifier), "transcript_id": str(transcript.id), "outcome": content["outcome"]}

                await progress("committing", 95)
                await database_call(tasks.complete_with, task_id, {}, store)
    except Exception as error:
        return await terminal(service, task_id, error)
    return await terminal(service, task_id)


def timing_windows(bounds, limits):
    merged = []
    for start, end in sorted(bounds):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    cap = min((settings.ASR_MAX_UPLOAD_MB * 1024**2 - 4096) / 32000,
              (limits.get("max_temp_bytes", settings.CLIPS_EXPORT_MAX_TEMP_MB * 1024**2) - 4096) / 32000)
    if cap <= 1:
        raise AppException("CLIP_TIMING_BUDGET_EXCEEDED", "定位音频上传或临时空间预算不足", 422)
    windows = []
    for start, end in merged:
        while end - start > cap:
            windows.append((start, start + cap))
            start += cap - min(2, cap / 4)
            if len(windows) > 32:
                raise AppException("CLIP_TIMING_BUDGET_EXCEEDED", "定位请求数超过预算", 422)
        windows.append((start, end))
    if (not windows or len(windows) > 32 or
            sum(b - a for a, b in windows) > limits["max_timing_seconds"] + 1e-6 or
            len(windows) * .5 > limits["timing_timeout"]):
        raise AppException("CLIP_TIMING_BUDGET_EXCEEDED", "整批定位超过音频、请求数或时间预算，请减少选择", 422)
    return windows


async def prepare_timeline(task, service, source, info, directory, selected, adapter, provider, controls, progress, on_work=None):
    payload, limits = task.input_payload, task.input_payload["allowance"]["clip_limits"]
    keys = {candidate["id"]: candidate_key(task.user_id, payload, candidate, info["sha256"]) for candidate in selected}
    cached = await database_call(service.repository.timelines_for_keys, task.user_id, list(keys.values()))
    cores = {identifier: cached[key]["item"] for identifier, key in keys.items() if key in cached}
    missing = [x for x in selected if x["id"] not in cores]
    plan = await database_call(service.repository.plan, task.user_id, UUID(payload["plan_id"]))
    if not missing:
        return {"items": playback_ranges([cores[x["id"]] for x in selected], plan["spec"]["strategy"], info["duration"], max_total_seconds=limits.get("max_export_seconds", 450)),
                "media": info, "audio_seconds": 0, "provider_model": payload["timing_model"], "version": TIMING_VERSION}
    if not provider.available or not provider.model:
        raise AppException("CLIP_TIMING_NOT_CONFIGURED", "请配置支持真实分段时间戳的切片定位模型", 503)
    offset = info["audio_start"] - info["origin"] if plan["source_basis"] == "asr" else 0
    bounds = {}
    for candidate in missing:
        core_start, core_end = candidate["start"] + offset, candidate["end"] + offset
        if core_start < info.get("clip_start", 0) - .1 or core_end > info["duration"] + .1:
            raise AppException("CLIP_RANGE_UNAVAILABLE", "候选落在没有共同音视频的区间，请选择其他候选", 422)
        start = max(info["audio_start"] - info["origin"], 0, core_start - 5)
        end = min(info["duration"], core_end + 5)
        if end <= start:
            raise AppException("CLIP_TIMING_UNRELIABLE", "候选超出源视频音轨范围", 422)
        bounds[candidate["id"]] = (start, end)
    windows = timing_windows(list(bounds.values()), limits)
    window_keys = {bounds: window_key(task.user_id, payload, info["sha256"], *bounds) for bounds in windows}
    window_cache = await database_call(service.repository.timelines_for_keys, task.user_id, list(window_keys.values()))
    requested = sum(end - start for start, end in windows if window_keys[(start, end)] not in window_cache)
    if controls.get("check_capacity"):
        controls["check_capacity"](int(max(end - start for start, end in windows) * 32000) + 4096)
    segments, items, pending_windows = [], [], []
    # All deterministic budgets above are validated before the first paid request.
    async with asyncio.timeout(limits["timing_timeout"]):
        for index, (start, end) in enumerate(windows):
            await progress("aligning", 20 + int(index / len(windows) * 25))
            audio_path = directory / f"timing-{index}.wav"
            preserve_audio = False
            try:
                cached_window = window_cache.get(window_keys[(start, end)])
                if cached_window:
                    window_segments = cached_window["segments"]
                else:
                    if on_work:
                        await on_work()
                    await adapter.audio_window(source, audio_path, info, start, end, directory, **controls)
                    if audio_path.stat().st_size > settings.ASR_MAX_UPLOAD_MB * 1024**2:
                        raise AppException("CLIP_TIMING_BUDGET_EXCEEDED", "定位音频超过供应商上传限制", 422)
                    data = await transcribe_window(provider, audio_path, None, controls["is_cancel_requested"])
                    window_segments = data.segments
                if len(window_segments) > 500 or len(segments) + len(window_segments) > 1000:
                    raise AppException("CLIP_TIMING_UNRELIABLE", "定位分段超过计算预算", 422)
                for segment in window_segments:
                    if segment["start"] < 0 or segment["end"] > end - start + .1:
                        raise AppException("CLIP_TIMING_UNRELIABLE", "定位时间超出音频窗口", 422)
                    current = {**segment, "start": start + segment["start"], "end": start + segment["end"]}
                    if not any(x["text"] == current["text"] and
                               min(x["end"], current["end"]) > max(x["start"], current["start"])
                               for x in segments):
                        segments.append(current)
                if not cached_window:
                    pending_windows.append((window_keys[(start, end)], window_segments))
            except BaseException as error:
                preserve_audio = isinstance(error, AppException) and error.code == "PROCESS_CLEANUP_FAILED"
                raise
            finally:
                from app.infrastructure.process_runner import ProcessRunner
                if not preserve_audio and ProcessRunner.directory_stopped(directory):
                    audio_path.unlink(missing_ok=True)
        segments.sort(key=lambda x: x["start"])
        for candidate in missing:
            left, right = bounds[candidate["id"]]
            local = [x for x in segments if x["start"] >= left - .1 and x["end"] <= right + .1]
            a, b, match = locate(candidate["quote"], local, info["duration"])
            if a < info.get("clip_start", 0) - .001:
                raise AppException("CLIP_RANGE_UNAVAILABLE", "定位区间没有共同音视频", 422)
            items.append({"candidate_id": candidate["id"], "start": a, "end": b,
                          "match": match, "precision": "segment", "title": candidate["title"]})
    for item in items:
        cores[item["candidate_id"]] = item
    items = playback_ranges([cores[x["id"]] for x in selected], plan["spec"]["strategy"], info["duration"], max_total_seconds=limits.get("max_export_seconds", 450))
    content = {"items": items, "media": info, "provider_model": provider.model,
               "audio_seconds": requested, "version": TIMING_VERSION}
    def store(session):
        for key, data in pending_windows:
            service.repository.store_timeline(session, task.user_id, UUID(payload["plan_id"]), key,
                {"kind": "window", "segments": data, "version": TIMING_VERSION})
        for identifier, item in cores.items():
            service.repository.store_timeline(session, task.user_id, UUID(payload["plan_id"]), keys[identifier],
                {"kind": "candidate", "item": item, "media": info, "provider_model": payload["timing_model"],
                 "provider_base": payload["timing_base_url"], "version": TIMING_VERSION})
    await database_call(service.tasks.repository.perform_if_active, task.id, store)
    return content


async def lock_reused_asset(service, item, locks, identifiers, cancelled):
    from app.modules.media.service import MediaService
    identifier = UUID(item["file_id"])
    record = await database_call(service.media.get, identifier)
    path = service.storage.resolve_file(record.storage_key)
    async with wait_for_storage(lambda: service.storage.file_lock(path), is_cancel_requested=cancelled):
        # Transfer the lock to the batch scope so cleanup cannot delete a reused file before commit.
        # wait_for_storage owns its context, so acquire a second shared lock for that scope.
        locks.enter_context(service.storage.file_lock(path))
    if str(identifier) not in await MediaService(service.media, service.storage).available_ids([identifier]):
        raise AppException("CLIP_REQUOTE_REQUIRED", "复用成片已失效，请重新报价", 409)
    identifiers.append(identifier)


async def execute_export(task_id, service, sources=None, adapter=None, provider=None):
    async def start_options():
        try:
            task = await database_call(service.tasks.repository.get, task_id)
            cached = await service.cached_export(task.user_id, task.input_payload["export_key"])
            if cached is None:
                plan = await database_call(service.repository.plan, task.user_id, UUID(task.input_payload["plan_id"]))
                cached = await service.cached_selection(task.user_id, plan, task.input_payload["candidate_ids"], task.input_payload["profile"])
            return {"clip_cache_ready": cached is not None}
        except AppException as error:
            if error.code != "DATABASE_UNAVAILABLE":
                raise
            return {}  # Optional cache hint; the owned bootstrap still fences authoritative failures.
    return await run_owned(task_id, service, lambda task: _execute_export(task, service, sources, adapter, provider),
                           start_options=start_options)


async def _execute_export(task, service, sources=None, adapter=None, provider=None):
    task_id = task.id
    tasks = service.tasks.repository
    log_queue_wait(task)
    payload, limits = task.input_payload, task.input_payload["allowance"]["clip_limits"]
    progress = Progress(service.tasks, task_id)
    directory = service.storage.root / "tmp" / str(task_id)
    promoted, preserve = [], False
    reuse_locks, reused_ids = ExitStack(), []

    async def cancelled():
        return await service.tasks.is_cancel_requested(task_id)

    async def actual_work():
        await database_call(tasks.clip_execution_mode, task_id, "render")

    async def on_progress(event):
        await progress("preparing_source", min(19, int(event.get("progress", 0) / 6)))

    controls = {"is_cancel_requested": cancelled, "on_progress": on_progress, "check_capacity": service.storage.check_capacity}
    try:
        enabled()
        async with asyncio.timeout(limits["export_timeout"]):
            await service.revalidate(task.user_id, task.task_type, payload)
            cached = await service.cached_export(task.user_id, payload["export_key"])
            if cached:
                await database_call(tasks.clip_execution_mode, task_id, "cache")
                await database_call(tasks.complete_with, task_id,
                    {"clip_export_id": str(cached["id"]), "clip_plan_id": payload["plan_id"], "cache_hit": True}, lambda session: None)
                return await terminal(service, task_id)
            plan = await database_call(service.repository.plan, task.user_id, UUID(payload["plan_id"]))
            selection = await service.cached_selection(task.user_id, plan, payload["candidate_ids"], payload["profile"])
            if selection:
                await database_call(tasks.clip_execution_mode, task_id, "cache")
                for item in selection["items"]:
                    await lock_reused_asset(service, item, reuse_locks, reused_ids, cancelled)
                identifier = uuid4()
                content = {"revision": payload["revision"], "items": selection["items"],
                    "source_sha256": selection["media"]["sha256"],
                    "timing_usage": {"audio_seconds": 0, "model": selection["provider_model"]}}
                def store_cached(session):
                    service.media.require_available(session, reused_ids, utcnow())
                    service.repository.register_export(session, task, identifier, content)
                    return {"clip_export_id": str(identifier), "clip_plan_id": payload["plan_id"],
                            "file_ids": [x["file_id"] for x in selection["items"]], "cache_hit": True}
                await database_call(tasks.complete_with, task_id, {}, store_cached)
                return await terminal(service, task_id)
            if payload.get("reuse_result") or payload.get("reuse_assets") or payload.get("_clip_execution_mode") == "cache":
                raise AppException("CLIP_REQUOTE_REQUIRED", "成片缓存失效，请重新报价", 409)
            if not payload.get("model_config") and (payload["timing_model"] != settings.CLIPS_TIMING_MODEL or payload["timing_base_url"] != settings.CLIPS_TIMING_API_BASE_URL):
                raise AppException("CLIP_REQUOTE_REQUIRED", "精细定位配置已改变，请重新报价", 409)
            if payload.get("model_config"):
                from app.modules.model_config.runtime import asr
                provider=await database_call(asr,"clip_timing",payload["model_config"],tasks.sessions)
                if provider.model!=payload["timing_model"]:raise AppException("CLIP_REQUOTE_REQUIRED","定位模型已更新，请重新确认",409)
            else:provider = provider or timing_provider(payload["timing_model"])
            if not provider.available or not provider.model:
                raise AppException("CLIP_TIMING_NOT_CONFIGURED", "请配置支持真实分段时间戳的切片定位模型", 503)
            plan = await database_call(service.repository.plan, task.user_id, UUID(payload["plan_id"]))
            selected = selected_candidates(plan, payload["candidate_ids"], payload["revision"])
            adapter = adapter or FFmpegClipAdapter(limits["render_timeout"], limits["max_temp_bytes"])
            if sources is None:
                from app.modules.clips.source import ClipSources
                from app.modules.download.adapter import YtDlpDownloadAdapter
                from app.infrastructure.process_runner import ProcessRunner
                from app.modules.uploads.service import upload_service
                sources = ClipSources(upload_service(service.repository.sessions, service.storage, service.tasks), service.media,
                    service.storage, YtDlpDownloadAdapter(ProcessRunner(settings.DOWNLOAD_TIMEOUT_SECONDS, limits["max_temp_bytes"])))
            async with wait_for_storage(lambda: service.storage.reservation(task_id, limits["max_temp_bytes"]),
                    is_cancel_requested=cancelled, on_wait=lambda: progress("waiting_for_storage", progress.value)):
                service.storage.create_task_directory(task_id)
                await progress("preparing_source", 5)
                async with sources.acquire(task, directory, **controls) as source:
                    info = await adapter.inspect(source, directory, **controls)
                    resource = payload["allowance"]["limits"]
                    if info.get("resource_duration", info["duration"]) > resource["video_seconds"] or source.stat().st_size > min(resource["file_bytes"], settings.MAX_FILE_SIZE_MB * 1024**2):
                        raise AppException("CLIP_SOURCE_LIMIT_EXCEEDED", "源视频超过当前媒体权益限制", 413)
                    if payload.get("content_sha256") and info["sha256"] != payload["content_sha256"]:
                        raise AppException("CLIP_SOURCE_MISMATCH", "源视频指纹与方案不一致", 409)
                    expected = payload.get("duration_seconds", 0)
                    measured = info.get("audio_duration", info["duration"]) if payload.get("source_kind") == "upload" else info.get("resource_duration", info["duration"])
                    if expected <= 0 or (not payload.get("content_sha256") and abs(measured - expected) > max(2, expected * .01)):
                        raise AppException("CLIP_SOURCE_MISMATCH", "源视频时长与报价不一致", 409)
                    with stage_timer("clip_alignment", task_id):
                        timeline = await prepare_timeline(task, service, source, info, directory, selected, adapter, provider, controls, progress, actual_work)
                    assets = await service.cached_assets(task.user_id, info, timeline["items"], payload["profile"])
                    files, records, results = [], [], []
                    expires = utcnow() + timedelta(hours=settings.STORAGE_FILE_TTL_HOURS)
                    for index, item in enumerate(timeline["items"]):
                        await progress("rendering", 50 + int(index / len(selected) * 35))
                        cached_asset = assets.get(item["candidate_id"])
                        if cached_asset:
                            await lock_reused_asset(service, cached_asset, reuse_locks, reused_ids, cancelled)
                            results.append({**cached_asset, **item})
                            continue
                        output, identifier = directory / f"clip-{index}.mp4", uuid4()
                        await actual_work()
                        with stage_timer("clip_render", task_id):
                            async with asyncio.timeout(limits["render_timeout"]):
                                verification = await adapter.render(source, output, info, item["start"], item["end"], **controls)
                        size = output.stat().st_size
                        if size <= 0 or size > min(resource["file_bytes"], settings.MAX_FILE_SIZE_MB * 1024**2):
                            raise AppException("FILE_TOO_LARGE", "切片成品超过单文件体积上限", 413)
                        key = f"files/{identifier}.mp4"
                        filename = safe_filename(item["title"], "mp4")
                        files.append((output, identifier, "mp4"))
                        records.append(MediaRecord(identifier, task_id, task.video_id, "clip_720_mp4", key, filename,
                            "video/mp4", size, expires, "clip", info["sha256"], item["start"], item["end"]))
                        results.append({**item, "asset_key": asset_key(task.user_id, info["sha256"], item, payload["profile"]),
                            "file_id": str(identifier), "filename": filename, "size_bytes": size,
                            "expires_at": expires.isoformat(), "verification": verification,
                            "download_url": f"/api/v1/files/{identifier}/download"})
                    if not records and not timeline["audio_seconds"]:
                        await database_call(tasks.clip_execution_mode, task_id, "cache")
                    if await cancelled():
                        raise AppException("TASK_CANCELLED", "任务已取消", 409)
                    await progress("committing", 95)
                    async with wait_for_storage(service.storage.lease, is_cancel_requested=cancelled):
                        if files:
                            promoted = service.storage.promote_batch(task_id, files, locked=True)
                    identifier = uuid4()
                    content = {"revision": payload["revision"], "items": results, "source_sha256": info["sha256"],
                               "timing_usage": {"audio_seconds": timeline["audio_seconds"], "model": timeline["provider_model"]}}

                    def store(session):
                        service.media.require_available(session, reused_ids, utcnow())
                        for record in records:
                            service.media.register(session, record)
                        service.repository.register_export(session, task, identifier, content)
                        return {"clip_export_id": str(identifier), "clip_plan_id": payload["plan_id"],
                                "file_ids": [x["file_id"] for x in results]}

                    await database_call(tasks.complete_with, task_id, {}, store)
    except Exception as error:
        preserve = isinstance(error, AppException) and error.code == "PROCESS_CLEANUP_FAILED"
        try:
            result = await terminal(service, task_id, error)
        except Exception:
            preserve = True  # Commit outcome unknown: retain manifest and files for reconciliation.
            raise
        return result
    finally:
        reuse_locks.close()
        if not preserve and directory.exists():
            try:
                current = await database_call(tasks.get, task_id)
                if current.status != "completed":
                    service.storage.discard_batch(task_id, await database_call(service.media.storage_keys))
                service.storage.remove_task_directory(task_id)
            except Exception:
                pass  # Retain manifest; independent cleanup/reconcile can prove the outcome later.
    return await terminal(service, task_id)


@celery_app.task(name="clips.execute")
def clips_task(task_id):
    async def execute():
        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1,
            socket_timeout=1, retry=Retry(NoBackoff(), 0))
        try:
            tasks = TaskService(TaskRepository(SessionLocal), ProgressBus(redis))
            service = clip_service(SessionLocal, tasks=tasks)
            task = await database_call(tasks.repository.get, UUID(task_id))
            function = execute_plan if task.task_type == "ai_clip_plan" else execute_export
            return await function(UUID(task_id), service)
        finally:
            await redis.aclose()
    return asyncio.run(execute())
