import math
import re

from app.modules.ai.providers.base import LLMRequest, LLMResult
from app.modules.ai.schemas import format_timestamp


class HeuristicLLMProvider:
    name = "heuristic"
    model = "heuristic-v1"

    async def generate(self, request: LLMRequest) -> LLMResult:
        context = request.context
        chinese = request.output_language.startswith("zh")
        if request.purpose == "question":
            evidence = context.get("evidence", [])
            content = {"answer": "本地摘录模式，仅展示相关原文：" + (evidence[0]["quote"] if evidence else "未找到依据"),
                "evidence_ids": [1] if evidence else [], "insufficient_evidence": not bool(evidence)}
        elif request.purpose == "mindmap":
            summary = context["summary"]
            content = {"title": summary["overview"][:100], "children": [
                {"title": chapter["title"], "children": [{"title": chapter["summary"][:500], "children": []}]}
                for chapter in summary["chapters"][:40]]}
        else:
            segments = context.get("segments") or [
                {"start": item.get("start_seconds", 0), "text": item["overview"] + " " + " ".join(item.get("key_points", []))}
                for item in context.get("summaries", [])]
            sentences = []
            for item in segments:
                sentences.extend(text.strip()[:500] for text in re.split(r"(?<=[。！？.!?])\s*|\n+", item["text"]) if text.strip())
            points = list(dict.fromkeys(sentences))[:8] or ["无可用内容" if chinese else "No content"]
            chapters = []
            size = max(1, math.ceil(len(segments) / 4))
            for index in range(0, len(segments), size):
                group = segments[index:index + size]
                start = float(group[0].get("start", 0))
                chapters.append({"start_seconds": start, "timestamp": format_timestamp(start),
                                 "title": (f"章节 {len(chapters) + 1}：" if chinese else f"Chapter {len(chapters) + 1}: ") + group[0]["text"][:60],
                                 "summary": " ".join(item["text"] for item in group)[:1000]})
            content = {"overview": " ".join(points[:3])[:1200], "key_points": points,
                       "chapters": chapters, "source_basis": request.source_basis,
                       "conclusions": points[:2], "audience": [], "action_steps": [],
                       "evidence": [{"quote": segments[0]["text"][:700], "start_seconds": float(segments[0].get("start", 0)),
                                     "end_seconds": float(segments[0].get("end", segments[0].get("start", 0)))}] if segments else []}
        return LLMResult(content, 0, 0, 0, self.model)
