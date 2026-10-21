from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ParsedVideo:
    platform: str
    platform_video_id: str
    title: str
    duration: float | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    subtitle_languages: list[str] = field(default_factory=list)
    formats: list[dict[str, Any]] = field(default_factory=list)


class VideoParser(Protocol):
    async def parse(self, url: str) -> ParsedVideo: ...
