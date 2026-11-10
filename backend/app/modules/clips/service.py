from uuid import UUID

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.ai.providers import get_llm_provider
from app.modules.clips.schemas import ClipStrategy
from app.modules.clips.cache import RENDERER_VERSION, TIMING_VERSION
from app.modules.clips.selection import digest, plan_key, timeline_hash
from app.modules.tasks.service import database_call
from app.modules.transcript.service import TranscriptService
from app.modules.uploads.repository import UploadRepository
from app.modules.billing.policy import seconds


def timing_identity():
    if settings.MODEL_CONFIG_ENABLED:
        from app.modules.model_config.runtime import asr
        provider=asr("clip_timing")
        if provider is not None:return (provider.model if provider.available else "", "managed")
    return settings.CLIPS_TIMING_MODEL, settings.CLIPS_TIMING_API_BASE_URL


def enabled():
    if not settings.CLIPS_ENABLED:
        raise AppException("CLIPS_DISABLED", "智能切片尚未启用", 503)
    if not settings.ALLOWANCES_ENABLED:
        raise AppException("ALLOWANCES_DISABLED", "智能切片需要开启额度功能", 503)


def clip_spec(strategy, language, model):
    return {"strategy": ClipStrategy.model_validate(strategy).model_dump(), "language": language,
            "model": model, "prompt_version": settings.CLIPS_PROMPT_VERSION, "schema_version": settings.CLIPS_SCHEMA_VERSION}


def selected_candidates(plan, ids, revision):
    if revision != plan["revision"]:
        raise AppException("CLIP_REVISION_MISMATCH", "方案版本已变化，请重新报价", 409)
    choices = {x["id"]: x for x in plan["candidates"]}
    if not 1 <= len(ids) <= 5 or len(set(ids)) != len(ids) or any(x not in choices for x in ids):
        raise AppException("CLIP_SELECTION_INVALID", "请选择当前方案中 1–5 个不同候选", 422)
    return [choices[x] for x in sorted(ids)]


class ClipService:
    def __init__(self, repository, transcripts, videos, tasks, billing, resolver, storage, media):
        self.repository, self.transcripts, self.videos = repository, transcripts, videos
        self.tasks, self.billing, self.resolver, self.storage, self.media = tasks, billing, resolver, storage, media

    async def quote_plan(self, user, body):
        enabled()
        if body.transcript_id is not None:
            payload = await TranscriptService(None, self.tasks, self.transcripts).ai_source(body.transcript_id, user.id)
        else:
            from app.modules.video.sources import resolve_input
            payload = await resolve_input(self.resolver, body.url, body.upload_id, user.id)
            source = await database_call(self.videos.clip_source, UUID(payload["video_id"]), user.id)
            from app.modules.billing.policy import seconds
            payload["duration_seconds"] = seconds(payload.get("duration_seconds") or source["duration"])
        spec = clip_spec(body.strategy.model_dump(), body.language.lower(), (get_llm_provider("clip_analysis") if settings.MODEL_CONFIG_ENABLED else get_llm_provider()).model)
        payload.update(clip_spec=spec, output_language=body.language.lower(), language=None, preset_id="audio_mp3",
            content_version=payload.get("content_version", settings.TRANSCRIPT_CONTENT_VERSION))
        transcript = None
        if body.transcript_id:
            transcript = await database_call(self.transcripts.get, body.transcript_id)
        else:
            from app.modules.transcript.providers import get_asr_provider
            transcript = await database_call(self.transcripts.find_cache, UUID(payload["video_id"]), None,
                payload["content_version"], get_asr_provider().model)
        if transcript is not None:
            payload["clip_timeline_hash"] = timeline_hash(transcript)
            cached = await database_call(self.repository.cached_plan, user.id, plan_key(user.id, transcript, spec))
            if cached:
                # A purchased plan grants this exact transcript. Reuse never needs its original bytes.
                source = await TranscriptService(None, self.tasks, self.transcripts).ai_source(transcript.id, user.id)
                payload = {**source, "clip_timeline_hash": timeline_hash(transcript), "clip_spec": spec,
                    "output_language": body.language.lower(), "language": None, "preset_id": "audio_mp3",
                    "reuse_result": {"clip_plan_id": str(cached["id"]), "transcript_id": str(transcript.id),
                                     "outcome": cached["outcome"], "cache_hit": True}}
        return await database_call(self.billing.save_quote, user.id, "ai_clip_plan", payload)

    async def plan(self, user, identifier):
        plan = await database_call(self.repository.plan, user.id, identifier)
        try:
            source = await self.resolve_export_source(user.id, plan["video_id"])
            plan["source_available"] = source.get("media_type") == "video"
            plan["source_status"] = "available_upload" if source.get("source_kind") == "upload" else "public_link_requires_fetch"
            if source.get("source_kind") == "link":
                plan["source_available"] = None  # URL metadata is not a current network-availability check.
        except AppException as error:
            if error.code not in {"CLIP_SOURCE_REQUIRED", "UPLOAD_NOT_FOUND", "UPLOAD_EXPIRED", "UPLOAD_NOT_READY", "UPLOAD_FILE_MISSING", "CLIP_SOURCE_NOT_FOUND", "CLIP_VIDEO_REQUIRED"}:
                raise
            plan["source_available"] = False
            plan["source_status"] = "audio_only" if error.code == "CLIP_VIDEO_REQUIRED" else "source_required"
        plan["source_required"] = plan["source_available"] is False
        return plan

    async def resolve_export_source(self, user_id, video_id, upload_id=None):
        source = await database_call(self.videos.clip_source, video_id, user_id)
        if source["media_type"] != "video":
            raise AppException("CLIP_VIDEO_REQUIRED", "纯音频可分析精彩内容，视频导出需要真实视频文件", 422)
        if source["source_kind"] == "upload":
            identifier = UUID(upload_id) if upload_id else await database_call(UploadRepository(self.repository.sessions).ready_for_video, user_id, video_id)
            if identifier is None:
                raise AppException("CLIP_SOURCE_REQUIRED", "原视频已删除或过期，请重新上传同一文件", 409)
            payload = await self.resolver.resolve(upload_id=identifier, user_id=user_id)
            if payload["content_sha256"] != source["content_sha256"]:
                raise AppException("UPLOAD_SOURCE_MISMATCH", "源视频版本与方案不一致", 409)
            return payload
        return {**source, "duration_seconds": seconds(source["duration"])}

    async def cached_export(self, user_id, key):
        from app.modules.media.service import MediaService
        for item in await database_call(self.repository.exports_for_key, user_id, key):
            ids = [UUID(x["file_id"]) for x in item["items"]]
            if ids and len(await MediaService(self.media, self.storage).available_ids(ids)) == len(ids):
                return item
        return None

    async def cached_assets(self, user_id, info, items, profile):
        from app.modules.clips.cache import asset_key
        from app.modules.media.service import MediaService
        from app.modules.tasks.repository import utcnow
        keys = {x["candidate_id"]: asset_key(user_id, info["sha256"], x, profile) for x in items}
        assets = await database_call(self.repository.assets_for_keys, user_id, list(keys.values()), utcnow())
        ids = list({UUID(x["file_id"]) for choices in assets.values() for x in choices})
        available = await MediaService(self.media, self.storage).available_ids(ids)
        return {identifier: next((x for x in assets[key] if x["file_id"] in available), None)
                for identifier, key in keys.items()}

    async def cached_selection(self, user_id, plan, ids, profile):
        from app.modules.clips.cache import TIMING_VERSION
        from app.modules.clips.timing import playback_ranges
        candidates = await database_call(self.repository.candidate_timelines, user_id, plan["id"], ids,
            timing_identity()[0], timing_identity()[1], TIMING_VERSION)
        groups = {}
        expected_sha = (plan.get("source") or {}).get("content_sha256")
        for content in candidates:
            sha = content["media"]["sha256"]
            if expected_sha and sha != expected_sha:
                continue
            groups.setdefault(sha, {}).setdefault(content["item"]["candidate_id"], content)
        for group in groups.values():
            if not all(identifier in group for identifier in ids):
                continue
            info = group[ids[0]]["media"]
            items = playback_ranges([group[x]["item"] for x in ids], plan["spec"]["strategy"], info["duration"])
            assets = await self.cached_assets(user_id, info, items, profile)
            if all(assets.values()):
                return {"items": [{**assets[x["candidate_id"]], **x} for x in items], "media": info,
                        "audio_seconds": 0, "provider_model": timing_identity()[0]}
        return None

    async def quote_export(self, user, body):
        enabled()
        plan = await database_call(self.repository.plan, user.id, body.plan_id)
        selected_candidates(plan, body.candidate_ids, body.revision)
        key = digest([str(user.id), str(body.plan_id), body.revision, sorted(body.candidate_ids), body.profile,
                      f"{RENDERER_VERSION}:{TIMING_VERSION}", timing_identity()[0]])
        base = {"plan_id": str(body.plan_id), "revision": body.revision, "candidate_ids": sorted(body.candidate_ids),
            "profile": body.profile, "export_key": key, "video_id": str(plan["video_id"]),
            "timing_model": timing_identity()[0], "timing_base_url": timing_identity()[1],
            "duration_seconds": 0, "output_language": plan["spec"]["language"], "title": "精选切片"}
        cached = await self.cached_export(user.id, key)
        if cached:
            base["reuse_result"] = {"clip_export_id": str(cached["id"]), "clip_plan_id": str(body.plan_id), "cache_hit": True}
        else:
            selection = await self.cached_selection(user.id, plan, sorted(body.candidate_ids), body.profile)
            if selection:
                base["reuse_assets"] = {"file_ids": [x["file_id"] for x in selection["items"]],
                                        "source_sha256": selection["media"]["sha256"]}
                return await database_call(self.billing.save_quote, user.id, "clips_export", base)
            base.update(await self.resolve_export_source(user.id, plan["video_id"]))
            if not timing_identity()[0] or not timing_identity()[1] or (timing_identity()[1]!="managed" and not settings.CLIPS_TIMING_API_KEY.get_secret_value()):
                raise AppException("CLIP_TIMING_NOT_CONFIGURED", "导出前请配置支持真实分段时间戳的定位模型", 503)
        return await database_call(self.billing.save_quote, user.id, "clips_export", base)

    async def revalidate(self, user_id, action, payload):
        enabled()
        if action == "ai_clip_plan":
            if payload.get("source_kind") == "transcript":
                current = await TranscriptService(None, self.tasks, self.transcripts).ai_source(UUID(payload["transcript_id"]), user_id)
                if current["video_id"] != payload["video_id"] or current["transcript_content_hash"] != payload["transcript_content_hash"]:
                    raise AppException("TRANSCRIPT_SOURCE_MISMATCH", "文稿来源发生变化", 409)
                transcript = await database_call(self.transcripts.get, UUID(payload["transcript_id"]))
                if timeline_hash(transcript) != payload["clip_timeline_hash"]:
                    raise AppException("TRANSCRIPT_SOURCE_MISMATCH", "文稿时间轴发生变化", 409)
            elif payload.get("upload_id"):
                await self.resolver.resolve(upload_id=UUID(payload["upload_id"]), user_id=user_id)
            return
        plan = await database_call(self.repository.plan, user_id, UUID(payload["plan_id"]))
        selected_candidates(plan, payload["candidate_ids"], payload["revision"])
        if payload.get("reuse_result"):
            cached = await self.cached_export(user_id, payload["export_key"])
            if cached is None or str(cached["id"]) != payload["reuse_result"]["clip_export_id"]:
                raise AppException("CLIP_REQUOTE_REQUIRED", "已报价的缓存成片失效，请重新报价", 409)
        elif payload.get("reuse_assets"):
            cached = await self.cached_selection(user_id, plan, payload["candidate_ids"], payload["profile"])
            if cached is None or {"file_ids": [x["file_id"] for x in cached["items"]],
                                  "source_sha256": cached["media"]["sha256"]} != payload["reuse_assets"]:
                raise AppException("CLIP_REQUOTE_REQUIRED", "已报价的复用成片失效，请重新报价", 409)
        else:
            try:
                current = await self.resolve_export_source(user_id, plan["video_id"], payload.get("upload_id"))
            except AppException as error:
                if error.code.startswith("UPLOAD_") or error.code == "CLIP_SOURCE_REQUIRED":
                    raise AppException("CLIP_REQUOTE_REQUIRED", "已冻结的源文件不可用，请重新报价", 409) from None
                raise
            if current["video_id"] != payload["video_id"] or current.get("content_sha256") != payload.get("content_sha256"):
                raise AppException("CLIP_SOURCE_MISMATCH", "源文件发生变化，请重新报价", 409)
            if current.get("upload_id") != payload.get("upload_id"):
                raise AppException("CLIP_REQUOTE_REQUIRED", "原文件上传实例已变化，请重新报价", 409)

    async def export(self, user, identifier):
        item = await database_call(self.repository.export, user.id, identifier)
        from app.modules.media.service import MediaService
        available = await MediaService(self.media, self.storage).available_ids([UUID(x["file_id"]) for x in item["items"]])
        item["items"] = [{**x, "download_available": x["file_id"] in available} for x in item["items"]]
        return item


def clip_service(sessions, tasks=None, storage=None):
    from app.infrastructure.queue.progress_bus import ProgressBus
    from app.infrastructure.queue.redis_client import redis_client
    from app.infrastructure.storage.local import LocalStorageAdapter
    from app.modules.billing.repository import AllowanceRepository
    from app.modules.clips.repository import ClipRepository
    from app.modules.media.repository import MediaRepository
    from app.modules.tasks.repository import TaskRepository
    from app.modules.tasks.service import TaskService
    from app.modules.transcript.repository import TranscriptRepository
    from app.modules.video.parsers.cached import get_video_parser
    from app.modules.video.repository import VideoRepository
    from app.modules.video.resolver import VideoResolver
    tasks = tasks or TaskService(TaskRepository(sessions), ProgressBus(redis_client))
    storage = storage or LocalStorageAdapter(settings.storage_path, settings.STORAGE_MAX_QUOTA_MB, settings.STORAGE_MIN_FREE_MB)
    videos = VideoRepository(sessions)
    return ClipService(ClipRepository(sessions), TranscriptRepository(sessions), videos, tasks,
        AllowanceRepository(sessions), VideoResolver(get_video_parser(), videos), storage, MediaRepository(sessions))
