"""The public example is owned, reproducible and independent of private task data."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from app.modules.ai.mindmap import MindMapGenerator
from app.modules.ai.providers.heuristic import HeuristicLLMProvider
from app.modules.ai.summarizer import Summarizer
from app.modules.billing.policy import catalog

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def test_published_catalog_matches_the_backend_contract():
    published = json.loads((FRONTEND / "src/marketing/generated/catalog.json").read_text())
    assert published == catalog(), "Rebuild with uv run python -m scripts.export_marketing_content"


async def test_public_example_matches_its_source_and_actual_local_generation():
    source = json.loads((FRONTEND / "content/knowledge-loop.json").read_text())
    published = json.loads((FRONTEND / "src/marketing/generated/example.json").read_text())
    provider = HeuristicLLMProvider()
    segments = source["segments"]
    transcript = SimpleNamespace(segments=segments, content_text="\n".join(item["text"] for item in segments), source_type="subtitle")
    summary = await Summarizer(provider).summarize(transcript, "zh")
    mindmap = await MindMapGenerator(provider).generate(summary.content, "zh")
    assert published["summary"] == summary.content.model_dump()
    assert published["mindmap"] == mindmap.content.model_dump()
    assert published["segments"] == segments
    assert published["sourceHash"] == hashlib.sha256(json.dumps(segments, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert published == json.loads((FRONTEND / "public/media/examples/knowledge-loop/result.json").read_text())
