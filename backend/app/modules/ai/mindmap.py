from dataclasses import dataclass
import math
import re

from app.core.exceptions import AppException
from app.modules.ai.providers.base import LLMProvider, LLMRequest, Usage, generate
from app.modules.ai.schemas import MindMapNode, VideoSummarySchema


def repair_mindmap(raw: dict, fallback_title: str) -> MindMapNode:
    if not isinstance(raw, dict):
        raise AppException("LLM_INVALID_RESPONSE", "思维导图必须为 JSON 树", 502)
    if isinstance(raw.get("root"), dict) and "title" not in raw:
        raw = raw["root"]
    count = 0

    def node(value, depth):
        nonlocal count
        count += 1
        if count > 500 or depth > 8:
            raise AppException("MINDMAP_TOO_LARGE", "思维导图超过节点数或深度限制", 502)
        if isinstance(value, str):
            value = {"title": value}
        if not isinstance(value, dict):
            raise AppException("LLM_INVALID_RESPONSE", "思维导图节点格式无效", 502)
        title = value.get("title") or value.get("name") or fallback_title
        if not isinstance(title, str):
            raise AppException("LLM_INVALID_RESPONSE", "思维导图标题格式无效", 502)
        children = value.get("children", [])
        if not isinstance(children, list) or len(children) > 100:
            raise AppException("LLM_INVALID_RESPONSE", "思维导图子节点格式无效", 502)
        return {"title": " ".join(title.split())[:500] or fallback_title[:500],
                "children": [node(child, depth + 1) for child in children]}

    return MindMapNode.model_validate(node(raw, 0))


def mindmap_json_to_markdown(node: MindMapNode) -> str:
    # Escape model/source text so titles cannot inject headings, links or raw HTML.
    def text(value):
        value = " ".join(value.split())
        return re.sub(r"([\\`*_\[\]<>#])", r"\\\1", value)

    lines = []

    def visit(current, depth):
        prefix = "#" * (depth + 1) + " " if depth < 3 else "  " * (depth - 3) + "- "
        lines.append(prefix + text(current.title))
        for child in current.children:
            visit(child, depth + 1)

    visit(node, 0)
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class GeneratedMindMap:
    content: MindMapNode
    markdown: str
    usage: Usage


class MindMapGenerator:
    def __init__(self, provider: LLMProvider):
        self.provider = provider

    async def generate(self, summary: VideoSummarySchema, language: str, *, cancelled=None) -> GeneratedMindMap:
        # Bounded summary preparation rather than sending an unbounded transcript.
        # Group the entire timeline into at most 40 branches, including the final chapters.
        size = max(1, math.ceil(len(summary.chapters) / 40))
        chapters = []
        for offset in range(0, len(summary.chapters), size):
            group = summary.chapters[offset:offset + size]
            budget = max(8, 160 // len(group))
            chapters.append({"title": group[0].title[:100], "start_seconds": group[0].start_seconds,
                             "summary": " / ".join(item.summary[:budget] for item in group)})
        compact = {"overview": summary.overview[:1200], "key_points": [point[:150] for point in summary.key_points[:8]],
                   "chapters": chapters}
        reply = await generate(self.provider, LLMRequest("mindmap", {"summary": compact}, MindMapNode.model_json_schema(),
                                                       language, summary.source_basis), cancelled)
        node = repair_mindmap(reply.content, summary.overview[:100])
        return GeneratedMindMap(node, mindmap_json_to_markdown(node), Usage().add(reply))
