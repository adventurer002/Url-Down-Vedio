"""Versioned cache identities; user scope and exact bytes/ranges are mandatory."""
from app.modules.clips.selection import digest

TIMING_VERSION = "timing-v3"
RENDERER_VERSION = "renderer-v2"


def candidate_key(user_id, payload, candidate, source_sha):
    return digest([str(user_id), "candidate", payload["plan_id"], payload.get("revision", 1), candidate,
                   source_sha, payload["timing_model"], payload["timing_base_url"], TIMING_VERSION])


def window_key(user_id, payload, source_sha, start, end):
    return digest([str(user_id), "window", source_sha, start, end,
                   payload["timing_model"], payload["timing_base_url"], TIMING_VERSION])


def asset_key(user_id, source_sha, item, profile):
    return digest([str(user_id), "asset", source_sha, round(item["start"], 6), round(item["end"], 6),
                   TIMING_VERSION, profile, RENDERER_VERSION])
