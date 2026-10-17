"""Agent-authored clip selections and YouTube chapter serialization."""

from pydantic import BaseModel, Field, model_validator


class Chapter(BaseModel):
    start_seconds: int = Field(ge=0, strict=True)
    title: str = Field(min_length=1)


class ClipSelection(BaseModel):
    """The render contract, independent of editorial style or channel policy."""

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    thumbnail_prompt: str = Field(min_length=1)
    chapters: list[Chapter] = Field(default_factory=list)
    score: float | None = None

    @model_validator(mode="after")
    def ordered_interval(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must follow start_ms.")
        if any(
            c.start_seconds >= (self.end_ms - self.start_ms) / 1000
            for c in self.chapters
        ):
            raise ValueError("A chapter extends beyond its clip.")
        return self


def _clock(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def chapters_text(chapters: list[dict]) -> str:
    return "\n".join(f"{_clock(c['start_seconds'])} {c['title']}" for c in chapters)
