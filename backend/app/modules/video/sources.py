"""Public URL and private upload inputs share an explicit resolution contract."""
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(default=None, min_length=1, max_length=8192)
    upload_id: UUID | None = None

    @model_validator(mode="after")
    def exactly_one_source(self):
        if (self.url is None) == (self.upload_id is None):
            raise ValueError("必须且只能提供 url 或 upload_id")
        return self


class SourceResolver(Protocol):
    async def resolve(self, url: str | None = None, *, upload_id: UUID | None = None,
                      user_id: UUID | None = None) -> dict: ...


async def resolve_input(resolver: SourceResolver, url, upload_id=None, user_id=None):
    # Preserve the original URL adapter contract for existing integrations.
    if upload_id is None:
        return await resolver.resolve(url)
    return await resolver.resolve(upload_id=upload_id, user_id=user_id)


class AIInputRequest(SourceRequest):
    transcript_id: UUID | None = None

    @model_validator(mode="after")
    def exactly_one_source(self):
        if sum(value is not None for value in (self.url, self.upload_id, self.transcript_id)) != 1:
            raise ValueError("必须且只能提供 url、upload_id 或 transcript_id")
        return self
