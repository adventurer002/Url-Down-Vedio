"""Rebuild owned public demo artifacts through the existing local generation services.

No database, network, cloud model, or user content is used.
"""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from app.modules.ai.mindmap import MindMapGenerator
from app.modules.ai.providers.heuristic import HeuristicLLMProvider
from app.modules.ai.summarizer import Summarizer
from app.modules.billing.policy import catalog

ROOT = Path(__file__).resolve().parents[2]


def timestamp(seconds, separator=","):
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}{separator}000"


async def main():
    source = json.loads((ROOT / "frontend/content/knowledge-loop.json").read_text())
    provider = HeuristicLLMProvider()
    segments = source["segments"]
    transcript = SimpleNamespace(segments=segments, content_text="\n".join(item["text"] for item in segments), source_type="subtitle")
    summary = await Summarizer(provider).summarize(transcript, "zh")
    mindmap = await MindMapGenerator(provider).generate(summary.content, "zh")
    result = {
        **source, "sourceType": "subtitle", "duration": 48,
        "provider": provider.name, "model": provider.model,
        "sourceHash": hashlib.sha256(json.dumps(segments, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "summary": summary.content.model_dump(), "mindmap": mindmap.content.model_dump(),
    }
    destination = ROOT / "frontend/src/marketing/generated"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "example.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (destination / "catalog.json").write_text(json.dumps(catalog(), ensure_ascii=False, indent=2) + "\n")
    media = ROOT / "frontend/public/media/examples/knowledge-loop"
    media.mkdir(parents=True, exist_ok=True)
    (media / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (media / "source.srt").write_text("\n\n".join(
        f"{index}\n{timestamp(item['start'])} --> {timestamp(item['end'])}\n{item['text']}"
        for index, item in enumerate(segments, 1)) + "\n")
    (media / "source.vtt").write_text("WEBVTT\n\n" + "\n\n".join(
        f"{timestamp(item['start'], '.')} --> {timestamp(item['end'], '.')}\n{item['text']}"
        for item in segments) + "\n")
    (media / "mindmap.md").write_text(mindmap.markdown)
    print("Exported owned subtitle example, local summary/mindmap, and public catalog snapshot.")


if __name__ == "__main__":
    asyncio.run(main())
