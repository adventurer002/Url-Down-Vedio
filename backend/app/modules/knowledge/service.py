from types import SimpleNamespace
from uuid import UUID

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.ai.providers import get_llm_provider
from app.modules.knowledge.questions import QUESTION_VERSION, question_key
from app.modules.tasks.service import database_call


class KnowledgeService:
    def __init__(self, repository, transcripts, results, videos, tasks, billing):
        self.repository, self.transcripts, self.results = repository, transcripts, results
        self.videos, self.tasks, self.billing = videos, tasks, billing

    async def readable_transcript(self, identifier, user):
        from app.modules.transcript.service import TranscriptService
        data = await TranscriptService(None, self.tasks, self.transcripts).get(identifier, user)
        return SimpleNamespace(**data)

    async def quote(self, user, identifier, question, language):
        if not settings.ALLOWANCES_ENABLED:
            raise AppException("ALLOWANCES_DISABLED", "视频问答需要开启额度功能", 503)
        transcript = await self.readable_transcript(identifier, user)
        if len(transcript.content_text) > 60000:
            raise AppException("TRANSCRIPT_TOO_LONG", "当前问答最多支持 60000 字符文稿", 413)
        video = (await database_call(self.videos.history_metadata, [transcript.video_id]))[transcript.video_id]
        model = get_llm_provider().model
        key = question_key(transcript, question, model, language)
        payload = {"video_id": str(transcript.video_id), "url": video["source_url"], "title": video["title"],
            "duration_seconds": max(1, int(video["duration"] or 1)), "transcript_id": str(identifier),
            "question": question, "question_key": key, "question_version": QUESTION_VERSION, "output_language": language}
        cached = await database_call(self.repository.cached_answer, user.id, key)
        if cached:
            payload["reuse_result"] = {"answer_id": str(cached["id"]), "transcript_id": str(identifier), "reused": True}
        return await database_call(self.billing.save_quote, user.id, "ai_question", payload)

    async def save(self, user, identifier, result_id, tags):
        transcript = await self.readable_transcript(identifier, user)
        if len(transcript.content_text) > 60000:
            raise AppException("TRANSCRIPT_TOO_LONG", "首版收藏最多支持 60000 字符文稿", 413)
        summary = ""
        if result_id:
            result = await database_call(self.results.get, result_id)
            await self.tasks.authorize_artifact("result_id", result_id, result.task_id, user)
            if result.transcript_id != identifier or result.result_type != "summary":
                raise AppException("RESULT_MISMATCH", "只能收藏当前文稿对应的总结", 422)
            content = result.content_json
            sections = [content["overview"]]
            for key, heading in (("conclusions", "核心结论"), ("key_points", "关键要点"),
                                 ("audience", "适合谁阅读"), ("action_steps", "行动步骤")):
                if content.get(key):
                    sections.append(heading + "\n" + "\n".join("• " + value for value in content[key]))
            if content.get("chapters"):
                sections.append("章节\n" + "\n".join(chapter["title"] + "：" + chapter["summary"] for chapter in content["chapters"]))
            summary = "\n\n".join(sections)
        metadata = await database_call(self.videos.history_metadata, [transcript.video_id])
        return await database_call(self.repository.save, user.id, transcript, metadata[transcript.video_id], summary, result_id, tags)
