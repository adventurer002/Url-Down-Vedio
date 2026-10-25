"""Source citations, private collections and metered Q&A. No paid API calls."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from test_allowances import billing
from test_allowances_postgres import postgres
from test_ai_module import seed
from test_download import video
from test_tasks import repository, MemoryBus
from app.core.config import settings
from app.core.exceptions import AppException
from app.main import create_app
from app.modules.ai.providers import HeuristicLLMProvider
from app.modules.ai.providers.base import LLMResult, Usage
from app.modules.ai.repository import AIRepository
from app.modules.ai.summarizer import repair_summary
from app.modules.auth.dependencies import required_user
from app.modules.billing.service import BillingService
from app.modules.knowledge.models import VideoAnswer, SavedVideo
from app.modules.knowledge.questions import answer_question, question_key, retrieve
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.knowledge.router import get_knowledge_service
from app.modules.knowledge.service import KnowledgeService
from app.modules.knowledge.worker import execute_question
from app.modules.tasks.service import TaskService
from app.modules.transcript.data import make_transcript
from app.modules.transcript.repository import TranscriptRepository


@pytest.fixture
def knowledge(billing):
    b = billing
    task = b.create("transcription")
    b.tasks.start(task.id)
    transcripts = TranscriptRepository(b.tasks.sessions)
    def save(session):
        data = make_transcript("zh", [
            {"start": 0.0, "end": 10.0, "text": "缓存键应包含模型和提示词版本，避免旧结果被错误复用。"},
            {"start": 10.0, "end": 20.0, "text": "数据库事务保证额度只结算一次，失败时释放占用。"}])
        item = transcripts.store(session, b.video_id, task.id, "asr", data, "v1", "test-model")
        return {"transcript_id": str(item.id)}
    completed = b.tasks.complete_with(task.id, {}, save)
    b.transcript = transcripts.get(UUID(completed.result_payload["transcript_id"]))
    b.knowledge = KnowledgeRepository(b.tasks.sessions)
    b.service = KnowledgeService(b.knowledge, transcripts, AIRepository(b.tasks.sessions), b.videos,
        TaskService(b.tasks, MemoryBus()), b.quotas)
    b.transcripts = transcripts
    return b


async def question_task(b, question="缓存键应包含哪些版本？"):
    quote = await b.service.quote(b.user, b.transcript.id, question, "zh")
    action, payload, _ = b.quotas.quote(b.user.id, UUID(quote["quote_id"]))
    return b.tasks.create(action, b.video_id, payload, user_id=b.user.id), quote


async def test_question_free_asr_metered_once_and_repeat_owned_answer(knowledge):
    b = knowledge
    task, quote = await question_task(b)
    assert quote["maximum"]["asr_seconds"] == 0 and quote["maximum"]["ai_generations"] == 1
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["held"] == 0
    before = b.quotas.balance(b.user.id)["balances"]
    output = await execute_question(task.id, b.service.tasks, b.knowledge, b.transcripts, HeuristicLLMProvider())
    assert output["status"] == "completed"
    result = b.tasks.get(task.id).result_payload
    answer = b.knowledge.answer(b.user.id, UUID(result["answer_id"]))
    assert answer["citations"][0]["quote"] in b.transcript.content_text
    assert answer["citations"][0]["approximate"] is True
    after = b.quotas.balance(b.user.id)["balances"]
    assert after["ai_generations"]["available"] == before["ai_generations"]["available"] - 1
    assert after["asr_seconds"] == before["asr_seconds"]
    again, quote = await question_task(b)
    assert quote["maximum"]["ai_generations"] == 0
    assert again.input_payload["reuse_result"]["answer_id"] == result["answer_id"]
    # Worker rechecks the private result even when a second quote was made before completion.
    await execute_question(again.id, b.service.tasks, b.knowledge, b.transcripts, HeuristicLLMProvider())
    assert b.quotas.balance(b.user.id)["balances"] == after
    assert len(b.knowledge.answers(b.user.id, b.transcript.id)) == 1


@pytest.mark.parametrize("failure", ["bad_citation", "insufficient", "cancel", "provider", "no_match"])
async def test_question_failure_cancel_and_invalid_citations_release(knowledge, failure):
    b = knowledge
    task, _ = await question_task(b, "天气和股票是多少" if failure == "no_match" else "缓存键包含什么")
    async def reply(request):
        if failure == "cancel":
            b.tasks.request_cancel(task.id)
        if failure == "provider":
            raise AppException("LLM_TIMEOUT", "请求超时", 504)
        return LLMResult({"answer": "缓存包含模型版本", "evidence_ids": [999] if failure == "bad_citation" else [1],
            "insufficient_evidence": failure == "insufficient"}, 10, 20, 1, "test")
    provider = SimpleNamespace(name="test", model="test", generate=reply)
    before = b.quotas.balance(b.user.id)["balances"]
    await execute_question(task.id, b.service.tasks, b.knowledge, b.transcripts, provider)
    assert b.tasks.get(task.id).status in {"failed", "cancelled"}
    assert b.quotas.balance(b.user.id)["balances"] == before
    assert b.knowledge.answers(b.user.id, b.transcript.id) == []


async def test_private_answer_and_library_access_and_idempotent_save(knowledge):
    b = knowledge
    saved = await b.service.save(b.user, b.transcript.id, None, ["架构", "后端"])
    again = await b.service.save(b.user, b.transcript.id, None, ["学习"])
    assert saved["id"] == again["id"]
    assert b.knowledge.search(b.user.id, "缓存键", "", 20, 0)["total"] == 1
    assert b.knowledge.search(b.user.id, "", "学习", 20, 0)["total"] == 1
    assert b.knowledge.search(b.user.id, "%", "", 20, 0)["total"] == 0
    assert b.knowledge.search(b.user.id, "", "架构", 20, 0)["total"] == 0
    other = b.users.subscription("knowledge-other", True, login=True)
    with pytest.raises(AppException):
        await b.service.save(other, b.transcript.id, None, [])
    with pytest.raises(AppException):
        await b.service.quote(other, b.transcript.id, "缓存怎么用？", "zh")
    assert b.knowledge.search(other.id, "缓存", "", 20, 0)["total"] == 0
    with pytest.raises(AppException):
        b.knowledge.saved(other.id, saved["id"])
    task, _ = await question_task(b)
    await execute_question(task.id, b.service.tasks, b.knowledge, b.transcripts, HeuristicLLMProvider())
    identifier = UUID(b.tasks.get(task.id).result_payload["answer_id"])
    with pytest.raises(AppException):
        b.knowledge.answer(other.id, identifier)
    assert b.knowledge.answers(other.id, b.transcript.id) == []
    b.knowledge.hide(b.user.id, saved["id"])
    assert b.knowledge.search(b.user.id, "", "", 20, 0)["total"] == 0
    restored = await b.service.save(b.user, b.transcript.id, None, ["恢复"])
    assert restored["id"] == saved["id"]


async def test_api_auth_validation_search_and_quote_ownership(knowledge):
    b = knowledge
    app = create_app()
    app.dependency_overrides[get_knowledge_service] = lambda: b.service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/knowledge/library")).status_code == 401
        app.dependency_overrides[required_user] = lambda: b.user
        body = {"transcript_id": str(b.transcript.id), "tags": ["后端", " 后端 "]}
        response = await client.post("/api/v1/knowledge/library", json=body)
        assert response.status_code == 200 and response.json()["data"]["tags"] == ["后端"]
        listing = (await client.get("/api/v1/knowledge/library", params={"q": "缓存", "tag": "后端"})).json()["data"]
        assert listing["total"] == 1
        assert "content_text" not in listing["items"][0]
        body["tags"] = ["x" * 25]
        assert (await client.post("/api/v1/knowledge/library", json=body)).status_code == 422
        for question in [" ", "x" * 1001]:
            assert (await client.post("/api/v1/knowledge/questions/quotes", json={"transcript_id": str(b.transcript.id), "question": question})).status_code == 422
        quote = await client.post("/api/v1/knowledge/questions/quotes", json={"transcript_id": str(b.transcript.id), "question": "缓存怎么设计？"})
        assert quote.status_code == 200 and quote.json()["data"]["maximum"]["ai_generations"] == 1
        identifier = response.json()["data"]["id"]
        assert (await client.delete("/api/v1/knowledge/library/" + identifier)).status_code == 200
        assert (await client.get("/api/v1/knowledge/library/" + identifier)).status_code == 404


def test_retrieval_bounded_source_timestamps_and_summary_evidence():
    transcript = SimpleNamespace(id=uuid4(), content_hash="hash", source_type="asr", segments=[
        {"start": 40.0, "end": 160.0, "text": "缓存键包含模型版本。" * 1000}])
    evidence = retrieve(transcript, "缓存键如何设置")
    assert len(evidence) <= 8 and sum(len(item["quote"]) for item in evidence) <= 5600
    assert all(item["start"] == 40.0 and item["end"] == 160.0 for item in evidence)
    assert question_key(transcript, "模型  版本", "test", "zh") == question_key(transcript, "模型 版本", "test", "zh")
    summary = repair_summary({"evidence": [{"quote": "缓存键包含模型版本。", "start_seconds": 999}, {"quote": "凭空编造"}],
        "conclusions": ["缓存需要版本"], "action_steps": ["检查缓存键"]}, transcript.segments, "asr")
    assert len(summary.evidence) == 1 and summary.evidence[0].start_seconds == 40
    assert summary.conclusions == ["缓存需要版本"]


def test_postgres_simultaneous_collection_save_has_one_owned_record(postgres):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    sessions, tasks, quotas, user, create = postgres
    task = create("transcription")
    assert tasks.start(task.id)
    transcripts = TranscriptRepository(sessions)
    def write(session):
        item = transcripts.store(session, task.video_id, task.id, "subtitle", make_transcript("zh", [
            {"start": 0.0, "end": 20.0, "text": "事务保护并发收藏。"}]), "v1", "none")
        return {"transcript_id": str(item.id)}
    completed = tasks.complete_with(task.id, {}, write)
    transcript = transcripts.get(UUID(completed.result_payload["transcript_id"]))
    knowledge = KnowledgeRepository(sessions)
    video = {"title": "并发收藏验收", "source_url": "https://example.com/watch"}
    barrier = Barrier(2)
    def save(tag):
        barrier.wait(timeout=5)
        return knowledge.save(user.id, transcript, video, "", None, [tag])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["a", "b"]))
    assert results[0]["id"] == results[1]["id"]
    assert knowledge.search(user.id, "事务", "", 20, 0)["total"] == 1


async def test_question_on_owned_long_transcript_needs_no_new_download_entitlement(knowledge):
    b = knowledge
    task, _ = await question_task(b)
    payload = {key: value for key, value in task.input_payload.items() if key not in {"allowance", "quote_id"}}
    payload["duration_seconds"] = 3600
    quoted = b.quotas.save_quote(b.user.id, "ai_question", payload)
    action, confirmed, _ = b.quotas.quote(b.user.id, UUID(quoted["quote_id"]))
    question = b.tasks.create(action, b.video_id, confirmed, user_id=b.user.id)
    result = await execute_question(question.id, b.service.tasks, b.knowledge, b.transcripts, HeuristicLLMProvider())
    assert result["status"] == "completed"
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["available"] == 4
