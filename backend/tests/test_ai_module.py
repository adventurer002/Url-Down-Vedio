import asyncio
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from test_tasks import MemoryBus, repository
from test_download import video, storage
from test_media_and_transcript import task_for
from app.core.exceptions import AppException
from app.main import create_app
from app.modules.ai.models import AIResult
from app.modules.ai.providers import HeuristicLLMProvider
from app.modules.ai.providers.base import LLMRequest, LLMResult, Usage, generate
from app.modules.ai.providers.openai_compatible import OpenAICompatibleLLMProvider, parse_json_content
from app.modules.ai.schemas import VideoSummarySchema, MindMapNode
from app.modules.ai.summarizer import Summarizer, repair_summary, split_segments
from app.modules.ai.mindmap import MindMapGenerator, mindmap_json_to_markdown, repair_mindmap
from app.modules.ai.repository import AIRepository
from app.modules.ai.router import get_ai_service
from app.modules.ai.service import AIService
from app.modules.ai.worker import execute_ai
from app.modules.tasks.service import TaskService
from app.modules.transcript.data import make_transcript
from app.modules.transcript.repository import TranscriptRepository
from app.modules.transcript.worker import execute_transcription


def source(text="字幕讲解缓存设计。", count=3):
    return SimpleNamespace(source_type="subtitle", content_text=text * count,
        segments=[{"start": float(i * 10), "end": float(i * 10 + 9), "text": text} for i in range(count)])


def seed(repository, video):
    task = task_for(repository, video)
    repository.start(task.id)
    data = make_transcript("zh", source().segments)
    return repository.perform_if_active(task.id, lambda session: TranscriptRepository(repository.sessions).store(
        session, task.video_id, task.id, "subtitle", data, "v1", "none"))


async def test_heuristic_is_deterministic_and_schema_valid():
    provider = HeuristicLLMProvider()
    first = await Summarizer(provider).summarize(source(), "zh")
    second = await Summarizer(provider).summarize(source(), "zh")
    assert first == second
    assert first.content.source_basis == "subtitle"
    assert first.content.chapters[0].timestamp == "00:00:00"
    assert first.usage.input_tokens == first.usage.output_tokens == 0
    tree = await MindMapGenerator(provider).generate(first.content, "zh")
    assert tree.content.children and tree.markdown.startswith("# ")


async def test_long_transcript_hierarchical_map_reduce_keeps_all_chapters():
    requests = []
    base = HeuristicLLMProvider()
    async def reply(request):
        requests.append(request)
        return await base.generate(request)
    provider = SimpleNamespace(generate=reply)
    transcript = source("文本证据。" * 160, 12)
    chunks = split_segments(transcript.segments, 512)
    assert len(chunks) > 12
    assert all(sum(len(s["text"]) for s in chunk) <= 512 for chunk in chunks)
    assert "".join(s["text"] for c in chunks for s in c) == "".join(s["text"] for s in transcript.segments)
    output = await Summarizer(provider, chunk_chars=512).summarize(transcript, "zh")
    assert sum(r.purpose == "map" for r in requests) == len(chunks)
    assert sum(r.purpose == "reduce" for r in requests) > 1
    assert max(c.start_seconds for c in output.content.chapters) == 110
    assert output.content.overview


async def test_chunk_cap_fails_before_provider_call():
    provider = Mock(generate=AsyncMock())
    with pytest.raises(AppException, match="最大分段"):
        await Summarizer(provider, 256, 1).summarize(source("x" * 1000), "zh")
    provider.generate.assert_not_awaited()


def test_schema_repairs_missing_fields_and_invalid_timestamps():
    repaired = repair_summary({"chapters": [{"start_seconds": -1, "timestamp": "bad"},
        {"start_seconds": 999999}, {"timestamp": "00:99:10"}]}, source().segments, "asr")
    assert [c.start_seconds for c in repaired.chapters] == [0, 10, 20]
    assert repaired.source_basis == "asr" and repaired.overview and repaired.key_points
    with pytest.raises(ValidationError):
        VideoSummarySchema.model_validate({"overview": "x"})
    with pytest.raises(ValidationError):
        repaired.chapters[0].__class__(start_seconds=3.0, timestamp="00:00:04", title="x", summary="y")
    with pytest.raises(AppException):
        repair_summary([], source().segments, "subtitle")


def test_mindmap_markdown_and_limits():
    tree = MindMapNode(title="Root", children=[MindMapNode(title="One", children=[MindMapNode(title="Two", children=[
        MindMapNode(title="Three", children=[MindMapNode(title="Four")])])])])
    assert mindmap_json_to_markdown(tree) == "# Root\n## One\n### Two\n- Three\n  - Four\n"
    assert "\\<script\\>" in mindmap_json_to_markdown(MindMapNode(title="<script>"))
    assert repair_mindmap({"root": {"name": "Root", "children": ["leaf"]}}, "fallback").children[0].title == "leaf"
    raw = {"title": "deep"}
    for _ in range(10):
        raw = {"title": "x", "children": [raw]}
    with pytest.raises(AppException) as error:
        repair_mindmap(raw, "x")
    assert error.value.code == "MINDMAP_TOO_LARGE"


async def test_cloud_payload_usage_and_json_repair():
    def respond(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert body["response_format"] == {"type": "json_object"}
        assert body["model"] == "test-model" and body["temperature"] == 0
        return httpx.Response(200, json={"choices": [{"message": {"content": '```json\n{"overview":"ok",}\n```'}}],
            "model": "resolved-model", "usage": {"prompt_tokens": 12, "completion_tokens": 4}})
    provider = OpenAICompatibleLLMProvider("https://example.com/v1", "secret", "test-model", transport=httpx.MockTransport(respond))
    result = await provider.generate(LLMRequest("summary", {}, {}, "zh", "subtitle"))
    assert result.content == {"overview": "ok"} and result.input_tokens == 12 and result.output_tokens == 4
    assert result.model == "resolved-model"
    assert Usage().add(result).cost(Decimal("1"), Decimal("2")) == Decimal(".000020")
    with pytest.raises(AppException):
        parse_json_content("[]")


@pytest.mark.parametrize("status,attempts", [(401, 1), (503, 2)])
async def test_cloud_errors_have_bounded_retries_without_secrets(status, attempts):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, text="secret upstream detail")
    provider = OpenAICompatibleLLMProvider("https://example.com", "secret", "test", transport=httpx.MockTransport(respond))
    with pytest.raises(AppException) as error:
        await provider.generate(LLMRequest("map", {}, {}, "zh", "subtitle"))
    assert len(calls) == attempts and "secret" not in error.value.message


async def test_cloud_total_timeout_and_cooperative_cancel():
    async def slow(request):
        await asyncio.sleep(5)
    provider = OpenAICompatibleLLMProvider("https://example.com", "secret", "test", timeout=.03, transport=httpx.MockTransport(slow))
    request = LLMRequest("map", {}, {}, "zh", "subtitle")
    with pytest.raises(AppException) as error:
        await provider.generate(request)
    assert error.value.code == "LLM_TIMEOUT"
    with pytest.raises(AppException) as error:
        await generate(provider, request, AsyncMock(return_value=True))
    assert error.value.code == "TASK_CANCELLED"


def test_cache_exact_dimensions_and_idempotent_store(repository, video):
    transcript = seed(repository, video)
    repo = AIRepository(repository.sessions)
    def write(session):
        return repo.store(session, transcript.task_id, transcript, "summary", "heuristic", "heuristic-v1", "v1", "zh", {}, Usage())
    item = repository.perform_if_active(transcript.task_id, write)
    assert repository.perform_if_active(transcript.task_id, write).id == item.id
    key = [transcript.content_hash, "summary", "heuristic-v1", "v1", "zh"]
    assert repo.find_cache(*key).id == item.id
    for index, value in enumerate(["x" * 64, "mindmap", "other-model", "v2", "en"]):
        changed = key.copy(); changed[index] = value
        assert repo.find_cache(*changed) is None
    assert item.estimated_cost_usd == 0


async def test_worker_summary_mindmap_and_cache(repository, video):
    transcript = seed(repository, video)
    pipeline = SimpleNamespace(acquire=AsyncMock(return_value=transcript))
    repo = AIRepository(repository.sessions)
    provider = HeuristicLLMProvider()
    results = []
    for kind in ["summary", "summary", "mindmap", "mindmap"]:
        task = task_for(repository, video, "ai_" + kind, output_language="zh")
        output = await execute_ai(task.id, TaskService(repository, MemoryBus()), repo, pipeline, provider)
        assert output["status"] == "completed"
        results.append(repository.get(task.id).result_payload)
    assert results[0]["result_id"] == results[1]["result_id"] and results[1]["cache_hit"]
    assert results[2]["result_id"] == results[3]["result_id"] and results[3]["cache_hit"]
    assert repo.get(__import__('uuid').UUID(results[2]["result_id"])).markdown_export.startswith("# ")
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AIResult)) == 2


async def test_cancel_wins_final_cas_no_ai_result(repository, video):
    transcript = seed(repository, video)
    task = task_for(repository, video, "ai_summary", output_language="zh")
    base = HeuristicLLMProvider()
    async def finish_after_cancel(request):
        reply = await base.generate(request)
        repository.request_cancel(task.id)
        return reply
    provider = SimpleNamespace(name=base.name, model=base.model, generate=finish_after_cancel)
    output = await execute_ai(task.id, TaskService(repository, MemoryBus()), AIRepository(repository.sessions),
                             SimpleNamespace(acquire=AsyncMock(return_value=transcript)), provider)
    assert output["status"] == "cancelled"
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AIResult)) == 0


async def test_inline_stage5_subtitles_without_finishing_ai_parent(repository, video, storage):
    task = task_for(repository, video, "ai_summary", output_language="zh", language=None)
    service = TaskService(repository, MemoryBus())
    subtitles = Mock(fetch=AsyncMock(return_value=make_transcript("en", source().segments)))
    asr = SimpleNamespace(model="mock", transcribe=AsyncMock(side_effect=AssertionError("subtitle first")))
    # Cleanup has a public repository contract; use the real empty media repository.
    from app.modules.media.repository import MediaRepository
    async def acquire(task_id, on_progress):
        record = await execute_transcription(task_id, service, TranscriptRepository(repository.sessions), MediaRepository(repository.sessions),
            storage, subtitles, Mock(), asr, intermediate=True, intermediate_progress=on_progress)
        assert repository.get(task_id).status == "processing"
        return record
    output = await execute_ai(task.id, service, AIRepository(repository.sessions), SimpleNamespace(acquire=acquire), HeuristicLLMProvider())
    assert output["status"] == "completed"
    asr.transcribe.assert_not_awaited()
    assert not (storage.root / "tmp" / str(task.id)).exists()


async def test_api_202_and_result_404(repository, video):
    from test_media_and_transcript import payload
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=payload(repository, video)))
    service = AIService(resolver, TaskService(repository, MemoryBus(), Mock(enqueue=Mock())), AIRepository(repository.sessions))
    app = create_app(); app.dependency_overrides[get_ai_service] = lambda: service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for path in ["summaries", "mindmaps"]:
            response = await client.post("/api/v1/ai/" + path, json={"url": "https://example.com/watch", "language": "zh"})
            assert response.status_code == 202 and response.json()["data"]["status"] == "queued"
        response = await client.get("/api/v1/ai/results/" + str(uuid4()))
        assert response.status_code == 404 and response.json()["error"]["code"] == "AI_RESULT_NOT_FOUND"


@pytest.mark.parametrize("payload", [[], {"choices": [{"message": "bad"}]},
    {"choices": [{"message": {"content": "{}"}}], "usage": []}, {"choices": []}])
async def test_malformed_cloud_envelope_maps_to_structured_error(payload):
    # A missing/empty usage is accepted; a nonempty non-object usage is rejected.
    if isinstance(payload, dict) and payload.get("usage") == []:
        payload["usage"] = ["invalid"]
    provider = OpenAICompatibleLLMProvider("https://example.com", "secret", "test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    with pytest.raises(AppException) as error:
        await provider.generate(LLMRequest("summary", {}, {}, "zh", "subtitle"))
    assert error.value.code == "LLM_INVALID_RESPONSE"


async def test_mindmap_groups_include_end_of_long_timeline():
    from app.modules.ai.schemas import ChapterSummary
    summary = (await Summarizer(HeuristicLLMProvider()).summarize(source(), "zh")).content
    summary.chapters = [ChapterSummary(start_seconds=float(i), timestamp=f"00:{i // 60:02d}:{i % 60:02d}",
        title=str(i), summary=f"evidence-{i}") for i in range(90)]
    async def respond(request):
        chapters = request.context["summary"]["chapters"]
        assert len(chapters) <= 40
        assert "evidence-89" in chapters[-1]["summary"]
        return LLMResult({"title": "root"}, 1, 1, 1, "test")
    await MindMapGenerator(SimpleNamespace(generate=respond)).generate(summary, "zh")


async def test_ai_timeout_is_durable_failed_status(repository, video, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "AI_TASK_TIMEOUT_SECONDS", .03)
    task = task_for(repository, video, "ai_summary", output_language="zh")
    async def slow(*args):
        await asyncio.sleep(10)
    result = await execute_ai(task.id, TaskService(repository, MemoryBus()), AIRepository(repository.sessions),
        SimpleNamespace(acquire=slow), HeuristicLLMProvider())
    assert result["status"] == "failed" and repository.get(task.id).error_code == "AI_TIMEOUT"
