from pydantic import BaseModel, ConfigDict, Field


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)


class DownloadOption(BaseModel):
    id: str
    label: str
    container: str
    height: int | None = None
    estimated_size_bytes: int | None = None


class VideoMetadata(BaseModel):
    platform: str
    platform_video_id: str
    title: str
    duration: float | None
    thumbnail: str | None
    uploader: str | None
    has_subtitles: bool
    subtitle_languages: list[str]
    download_options: list[DownloadOption]


class ParseResponse(BaseModel):
    data: VideoMetadata
