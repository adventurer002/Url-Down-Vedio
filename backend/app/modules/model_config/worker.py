"""Bounded, cancellable probes using fixed fixtures and production business validators."""

import asyncio
from pathlib import Path
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4
import time, wave
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.queue.celery_app import celery_app
from app.infrastructure.queue.progress_bus import ProgressBus
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService, database_call
from app.modules.model_config.service import ModelConfigService
from app.modules.model_config.runtime import ConfiguredLLM, ConfiguredASR, configuration


class ModelTestDispatcher:
    def enqueue(self, task_id):
        celery_app.send_task("models.test", args=[str(task_id)], task_id=str(task_id))


async def run_checks(identifier, phase, factory, cancelled, progress):
    model, connection, _ = await database_call(configuration, identifier, factory)
    adapter = connection["adapter"]
    if model["family"] == "llm":
        from app.modules.ai.providers.base import LLMRequest, generate
        from app.modules.ai.providers.bounded import BoundedLLMProvider

        provider = ConfiguredLLM(identifier, factory)
        provider = BoundedLLMProvider(
            provider,
            {
                "max_calls": 8,
                "total_output_tokens": 12000,
                "output_tokens_per_call": 2048,
            },
        )
        if phase == "connection":
            reply = await generate(
                provider,
                LLMRequest(
                    "summary",
                    {"instruction": "Return ok=true."},
                    {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                    "en",
                    "subtitle",
                    256,
                ),
                cancelled,
            )
            if reply.content.get("ok") is not True:
                raise AppException(
                    "MODEL_SCHEMA_INVALID",
                    "接口可连接，但未返回有效 JSON 测试结果",
                    502,
                )
            return {
                "capabilities": [],
                "checks": ["authenticated_request", "json_object"],
            }
        from app.modules.ai.summarizer import Summarizer
        from app.modules.ai.mindmap import MindMapGenerator
        from app.modules.knowledge.questions import answer_question
        from app.modules.clips.selection import LLMClipSelector
        from app.modules.clips.service import clip_spec
        from app.modules.clips.schemas import ClipStrategy

        text = "会议每天九点开始。参加者先准备三个问题，再记录讨论结论。按时准备能减少等待时间。"
        transcript = SimpleNamespace(
            id=uuid4(),
            video_id=uuid4(),
            source_type="subtitle",
            content_text=text,
            content_hash="model-probe-v1",
            segments=[{"start": 0.0, "end": 20.0, "text": text}],
        )
        from app.modules.ai.providers.heuristic import HeuristicLLMProvider

        baseline = await Summarizer(HeuristicLLMProvider()).summarize(transcript, "zh")
        summary = baseline
        capabilities, failures = [], {}

        async def clip_progress(*args):
            pass

        for index, use in enumerate(
            ("summary", "mindmap", "question", "clip_analysis")
        ):
            try:
                if await cancelled():
                    raise AppException("TASK_CANCELLED", "模型测试已取消", 409)
                if use == "summary":
                    summary = await Summarizer(provider).summarize(
                        transcript, "zh", cancelled=cancelled
                    )
                elif use == "mindmap":
                    await MindMapGenerator(provider).generate(
                        summary.content, "zh", cancelled=cancelled
                    )
                elif use == "question":
                    await answer_question(
                        provider, transcript, "会议几点开始？", "zh", cancelled
                    )
                else:
                    await LLMClipSelector(provider).select(
                        transcript,
                        clip_spec(ClipStrategy().model_dump(), "zh", provider.model),
                        cancelled,
                        clip_progress,
                    )
                capabilities.append(use)
            except AppException as error:
                if error.code == "TASK_CANCELLED":
                    raise
                failures[use] = error.code
            await progress(20 + index * 20)
        if not capabilities:
            raise AppException(
                "MODEL_COMPATIBILITY_FAILED", "模型未通过任何业务能力测试", 502
            )
        return {
            "capabilities": capabilities,
            "checks": capabilities,
            "failures": failures,
        }
    provider = ConfiguredASR(identifier, factory)
    path = Path(__file__).with_name("assets") / "probe.wav"
    reply = await provider.transcribe(path, "en")
    if not reply.content_text.strip():
        raise AppException("MODEL_TRANSCRIPT_EMPTY", "转写测试未返回有效文稿", 502)
    if phase == "connection":
        return {
            "capabilities": [],
            "checks": ["authenticated_request", "audio_transcription"],
        }
    text = reply.content_text.casefold()
    if "meeting" not in text or not ("nine" in text or "9" in text):
        raise AppException(
            "MODEL_TRANSCRIPT_MISMATCH",
            "固定语音未被正确识别，请检查模型是否适合语音转写",
            502,
        )
    capabilities = ["transcription"]
    if adapter == "openai_asr":
        with wave.open(str(path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
        if not reply.segments or any(
            not 0 <= segment["start"] < segment["end"] <= duration + 0.5
            for segment in reply.segments
        ):
            raise AppException(
                "MODEL_TIMESTAMPS_INVALID", "转写结果缺少有效的分段时间戳", 502
            )
        capabilities.append("clip_timing")
    return {
        "capabilities": capabilities,
        "checks": [
            "fixed_speech_recognized",
            "segment_timestamps" if adapter == "openai_asr" else "coarse_timing_only",
        ],
    }


async def execute_test(task_id, service, configs, checks=run_checks):
    tasks = service.repository
    if not await database_call(tasks.start, task_id):
        return {"task_id": str(task_id)}
    task = await database_call(tasks.get, task_id)
    started = time.monotonic()

    async def cancelled():
        from app.modules.auth.repository import UserRepository

        user = await database_call(UserRepository(configs.sessions).get, task.user_id)
        return (
            not user
            or user.role != "admin"
            or user.status != "active"
            or user.auth_version != task.input_payload.get("admin_version")
        ) or await service.is_cancel_requested(task_id)

    async def progress(value):
        try:
            await service.bus.publish(task_id, "processing", value, stage="model_test")
        except AppException:
            pass

    from app.modules.model_config.runtime import probe_context

    context_marker = probe_context.set(True)
    work = None
    storage = None
    directory_marker = None
    capacity_marker = None
    storage_scope = ExitStack()
    try:
        spec = await database_call(configs.probe_spec, task.input_payload["probe_id"])
        await database_call(tasks.mark_processing, task_id)
        async with asyncio.timeout(900):
            if spec["family"] == "asr":
                from app.infrastructure.storage import LocalStorageAdapter
                from app.modules.transcript.providers import (
                    asr_work_directory,
                    asr_capacity_check,
                )

                storage = LocalStorageAdapter(
                    settings.storage_path,
                    settings.STORAGE_MAX_QUOTA_MB,
                    settings.STORAGE_MIN_FREE_MB,
                )
                storage_scope.enter_context(storage.reservation(task_id, 1024 * 1024))
                directory = storage.create_task_directory(task_id)
                directory_marker = asr_work_directory.set(directory)
                capacity_marker = asr_capacity_check.set(storage.check_capacity)
            work = asyncio.create_task(
                checks(
                    spec["revision_id"],
                    spec["phase"],
                    configs.sessions,
                    cancelled,
                    progress,
                )
            )
            while not work.done():
                if await cancelled():
                    raise AppException("TASK_CANCELLED", "模型测试已取消", 409)
                await asyncio.wait({work}, timeout=0.5)
            result = await work
            if storage:
                storage.remove_task_directory(task_id)
            task = await database_call(
                tasks.complete_with,
                task_id,
                {
                    **result,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "tested_at": datetime.now(timezone.utc).isoformat(),
                },
                lambda session: None,
            )
    except Exception as error:
        code = (
            error.code
            if isinstance(error, AppException)
            else "MODEL_TEST_TIMEOUT"
            if isinstance(error, TimeoutError)
            else "MODEL_TEST_FAILED"
        )
        task = await database_call(
            tasks.finish,
            task_id,
            "cancelled" if code == "TASK_CANCELLED" else "failed",
            0,
            error_code=code,
            error_message="模型测试未通过，请检查配置、服务额度或兼容性",
            result_payload={
                "capabilities": [],
                "duration_ms": round((time.monotonic() - started) * 1000),
                "tested_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    finally:
        if work and not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        if directory_marker is not None:
            from app.modules.transcript.providers import (
                asr_work_directory,
                asr_capacity_check,
            )

            asr_work_directory.reset(directory_marker)
            asr_capacity_check.reset(capacity_marker)
        if storage:
            try:
                storage.remove_task_directory(task_id)
            except Exception:
                pass  # Process-control checks preserve unknown/live process trees.
        storage_scope.close()
        probe_context.reset(context_marker)
    try:
        await service.bus.publish(
            task_id, task.status, task.progress, error_code=task.error_code
        )
    except AppException:
        pass
    return {"task_id": str(task_id), "status": task.status}


@celery_app.task(name="models.test")
def test_task(task_id):
    async def execute():
        from app.infrastructure.database.session import SessionLocal

        client = Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
            retry=Retry(NoBackoff(), 0),
        )
        try:
            return await execute_test(
                UUID(task_id),
                TaskService(TaskRepository(SessionLocal), ProgressBus(client)),
                ModelConfigService(SessionLocal),
            )
        finally:
            await client.aclose()

    return asyncio.run(execute())
