import json
import re

from app.core.exceptions import AppException
from app.modules.transcript.data import TranscriptData, make_transcript

TIME = r"(?:\d{1,3}:)?\d{2}:\d{2}[.,]\d{3}"
TIMING = re.compile(rf"^\s*({TIME})\s*-->\s*({TIME})")


def timestamp(text: str) -> float:
    values = text.replace(",", ".").split(":")
    if len(values) == 2:
        return int(values[0]) * 60 + float(values[1])
    return int(values[0]) * 3600 + int(values[1]) * 60 + float(values[2])


def parse_subtitle(content: str, extension: str, language: str) -> TranscriptData:
    segments = []
    try:
        if extension == "json3":
            events = json.loads(content).get("events", [])
            for index, item in enumerate(events):
                if not item.get("segs"):
                    continue
                start = float(item.get("tStartMs", 0)) / 1000
                duration = float(item.get("dDurationMs", 0)) / 1000
                end = start + duration
                if not duration and index + 1 < len(events):
                    end = max(start, float(events[index + 1].get("tStartMs", 0)) / 1000)
                text = "".join(segment.get("utf8", "") for segment in item["segs"])
                segments.append({"start": start, "end": end, "text": text})
        elif extension in {"vtt", "srt"}:
            current, lines = None, []

            def flush():
                if current is not None:
                    segments.append({"start": current[0], "end": current[1], "text": " ".join(lines)})

            for line in content.lstrip("\ufeff").replace("\r\n", "\n").split("\n") + [""]:
                match = TIMING.match(line)
                if match:
                    flush()
                    current = (timestamp(match[1]), timestamp(match[2]))
                    lines = []
                elif not line.strip():
                    flush()
                    current, lines = None, []
                elif current is not None:
                    lines.append(line.strip())
        else:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise AppException("SUBTITLE_INVALID", "字幕格式无效", 502) from None
    return make_transcript(language, segments)
