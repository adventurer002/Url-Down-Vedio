from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AppException
from app.modules.knowledge.models import SavedVideo, VideoAnswer


def answer_data(item):
    return {key: getattr(item, key) for key in ("id", "task_id", "transcript_id", "question", "answer", "citations",
        "model", "prompt_version", "output_language", "input_tokens", "output_tokens", "created_at")}


def saved_data(item, detail=True):
    value = {key: getattr(item, key) for key in ("id", "transcript_id", "result_id", "title", "video", "source_type", "tags", "created_at", "updated_at")}
    value.update(excerpt=(item.summary_text.split("\n", 1)[0] if item.summary_text else item.content_text)[:300])
    if detail:
        value.update(content_text=item.content_text, summary_text=item.summary_text)
    return value


class KnowledgeRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    def cached_answer(self, user_id, key):
        with self.sessions() as session:
            item = session.scalar(select(VideoAnswer).where(VideoAnswer.user_id == user_id, VideoAnswer.question_key == key))
            return answer_data(item) if item else None

    def answer(self, user_id, identifier):
        with self.sessions() as session:
            item = session.scalar(select(VideoAnswer).where(VideoAnswer.id == identifier, VideoAnswer.user_id == user_id))
            if item is None:
                raise AppException("ANSWER_NOT_FOUND", "回答不存在", 404)
            return answer_data(item)

    def answers(self, user_id, transcript_id):
        with self.sessions() as session:
            return [answer_data(item) for item in session.scalars(select(VideoAnswer).where(
                VideoAnswer.user_id == user_id, VideoAnswer.transcript_id == transcript_id)
                .order_by(VideoAnswer.created_at.desc(), VideoAnswer.id.desc()).limit(20))][::-1]

    def store_answer(self, session, user_id, task_id, payload, answer, citations, usage):
        existing = session.scalar(select(VideoAnswer).where(VideoAnswer.user_id == user_id,
            VideoAnswer.question_key == payload["question_key"]))
        if existing:
            return existing.id
        item = VideoAnswer(id=uuid4(), user_id=user_id, task_id=task_id, transcript_id=payload["transcript_uuid"],
            question_key=payload["question_key"], question=payload["question"], answer=answer, citations=citations,
            model=payload["allowance"]["llm_model"], prompt_version=payload["question_version"],
            output_language=payload["output_language"], input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
        session.add(item)
        session.flush()
        return item.id

    def save(self, user_id, transcript, video, summary_text, result_id, tags):
        with self.sessions.begin() as session:
            # Unique ownership key also arbitrates simultaneous saves on different API processes.
            conditions = (SavedVideo.user_id == user_id, SavedVideo.transcript_id == transcript.id)
            item = session.scalar(select(SavedVideo).where(*conditions).with_for_update())
            if item is None:
                try:
                    with session.begin_nested():
                        item = SavedVideo(id=uuid4(), user_id=user_id, transcript_id=transcript.id, result_id=result_id,
                            title=video["title"][:1000], video=video, source_type=transcript.source_type,
                            content_text=transcript.content_text, summary_text=summary_text, tags=[], tags_text="", search_text="")
                        session.add(item)
                        session.flush()
                except IntegrityError:
                    item = session.scalar(select(SavedVideo).where(*conditions).with_for_update())
                    if item is None:
                        raise
            if result_id:
                item.result_id, item.summary_text = result_id, summary_text
            item.tags = tags
            item.tags_text = "\n" + "\n".join(tag.casefold() for tag in tags) + "\n"
            item.search_text = "\n".join([item.title, item.content_text, item.summary_text, *tags]).casefold()
            item.hidden = False
            item.updated_at = datetime.now(timezone.utc)
            session.flush()
            return saved_data(item)

    def saved(self, user_id, identifier):
        with self.sessions() as session:
            item = session.scalar(select(SavedVideo).where(SavedVideo.id == identifier,
                SavedVideo.user_id == user_id, SavedVideo.hidden.is_(False)))
            if item is None:
                raise AppException("SAVED_VIDEO_NOT_FOUND", "收藏不存在", 404)
            return saved_data(item)

    def search(self, user_id, query, tag, limit, offset, transcript_id=None):
        conditions = [SavedVideo.user_id == user_id, SavedVideo.hidden.is_(False)]
        if transcript_id:
            conditions.append(SavedVideo.transcript_id == transcript_id)
        if query:
            conditions.append(SavedVideo.search_text.contains(query.casefold(), autoescape=True))
        if tag:
            conditions.append(SavedVideo.tags_text.contains("\n" + tag.casefold() + "\n", autoescape=True))
        with self.sessions() as session:
            total = session.scalar(select(func.count()).select_from(SavedVideo).where(*conditions))
            items = session.scalars(select(SavedVideo).where(*conditions).order_by(
                SavedVideo.updated_at.desc(), SavedVideo.id.desc()).limit(limit).offset(offset))
            return {"items": [saved_data(item, False) for item in items], "total": total, "limit": limit, "offset": offset}

    def hide(self, user_id, identifier):
        with self.sessions.begin() as session:
            item = session.scalar(select(SavedVideo).where(SavedVideo.id == identifier, SavedVideo.user_id == user_id).with_for_update())
            if item is None:
                raise AppException("SAVED_VIDEO_NOT_FOUND", "收藏不存在", 404)
            item.hidden = True
            return {"removed": True}
