import asyncio
from uuid import UUID

from app.core.exceptions import AppException
from app.modules.media.service import MediaService
from app.modules.tasks.service import TaskService, database_call
from app.modules.video.repository import VideoRepository


class TaskHistoryService:
    def __init__(self, tasks: TaskService, videos: VideoRepository, media: MediaService):
        self.tasks, self.videos, self.media = tasks, videos, media

    async def list(self, limit: int, offset: int):
        records, total = await database_call(self.tasks.repository.list_recent, limit, offset)
        videos = await database_call(self.videos.history_metadata, [t.video_id for t in records if t.video_id])
        async def snapshot(task):
            try:
                return await self.tasks.bus.snapshot(task.id)
            except AppException:
                return None
        try:
            async with asyncio.timeout(1.5):
                snapshots = await asyncio.gather(*(snapshot(task) for task in records))
        except TimeoutError:
            snapshots = [None] * len(records)
        ids = []
        for task in records:
            try:
                ids.append(UUID((task.result_payload or {}).get("file_id", "")))
            except (ValueError, TypeError, AttributeError):
                pass
        available = await self.media.available_ids(ids) if ids else set()
        items = []
        for task, cached in zip(records, snapshots):
            item = self.tasks.serialize(task, cached)
            video = videos.get(task.video_id)
            result = dict(item["result_payload"] or {})
            # A retained completed task does not imply its media file is still downloadable.
            downloadable = item["status"] == "completed" and result.get("file_id") in available
            if not downloadable:
                result.pop("file_id", None)
                result.pop("download_url", None)
            item.update(video=video, video_title=video["title"] if video else task.input_payload.get("title"),
                        download_available=downloadable, result_payload=result or None)
            # History needs no internal extractor/CLI arguments or original signed request payloads.
            item["input_payload"] = {k: v for k, v in task.input_payload.items() if k in {"preset_id", "language", "output_language"}}
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}
