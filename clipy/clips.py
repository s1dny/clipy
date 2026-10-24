"""Agent-authored clip selections and YouTube chapter serialization."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MIN_VIDEO_MS = 8 * 60 * 1000


class SourceAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    streamed_on: date
    evidence: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def no_em_dash(cls, value):
        if "\u2014" in value:
            raise ValueError("Em dashes are not allowed.")
        return value


class SourceRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    reason: str = Field(min_length=1)


class Annotation(BaseModel):
    """A brief card, timed against the FINAL edited video, in milliseconds."""

    model_config = ConfigDict(extra="forbid")
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    label: Literal["Context", "Recorded", "Uploaded", "Question", "Reference"]
    text: str = Field(min_length=1, max_length=700)
    evidence: str = Field(min_length=1)
    heading: str | None = Field(default=None, min_length=1, max_length=80)
    image_url: str | None = Field(default=None, pattern=r"^https://")
    image_credit: str | None = Field(default=None, min_length=1, max_length=500)
    position: Literal["top_left", "top_right", "bottom_left", "bottom_right"] = (
        "bottom_left"
    )

    @field_validator("text", "heading")
    @classmethod
    def no_em_dash(cls, value):
        if value and "\u2014" in value:
            raise ValueError("Em dashes are not allowed in on-screen text.")
        return value

    @model_validator(mode="after")
    def image_provenance(self):
        if self.image_url and not self.image_credit:
            raise ValueError("An image needs its source/credit.")
        return self


class VisualHold(BaseModel):
    """Hold an inspected source frame while the edited audio continues."""

    model_config = ConfigDict(extra="forbid")
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    source_ms: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=1)


class Chapter(BaseModel):
    start_seconds: int = Field(ge=0, strict=True)
    title: str = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def no_em_dash(cls, value):
        if "\u2014" in value:
            raise ValueError("Em dashes are not allowed in chapter titles.")
        return value


class ClipSelection(BaseModel):
    """The render contract, independent of editorial style or channel policy."""

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    thumbnail_prompt: str = Field(min_length=1)
    chapters: list[Chapter] = Field(default_factory=list)
    score: float | None = None
    keep: list[SourceRange] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    visual_holds: list[VisualHold] = Field(default_factory=list)
    source: SourceAttribution | None = None

    @field_validator("title", "description")
    @classmethod
    def no_em_dash(cls, value):
        if "\u2014" in value:
            raise ValueError("Em dashes are not allowed in video text.")
        return value

    def validate_release(self):
        if self.duration_ms < MIN_VIDEO_MS:
            raise ValueError(
                "Every video must be at least 8 minutes AFTER all edits. Keep worthwhile asides; do not pad a short clip."
            )
        if self.source is None:
            raise ValueError(
                "Every video needs source name, verified streamed_on date, and date evidence. An upload date is not a stream date."
            )

    @property
    def duration_ms(self) -> int:
        return (
            sum(r.end_ms - r.start_ms for r in self.keep)
            if self.keep
            else self.end_ms - self.start_ms
        )

    @model_validator(mode="after")
    def ordered_interval(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must follow start_ms.")
        previous = self.start_ms
        for span in self.keep:
            if not previous <= span.start_ms < span.end_ms <= self.end_ms:
                raise ValueError(
                    "keep ranges must be chronological, nonoverlapping and inside the source interval."
                )
            previous = span.end_ms
        previous = 0
        for card in self.annotations:
            if not previous <= card.start_ms < card.end_ms <= self.duration_ms:
                raise ValueError(
                    "Annotations must be chronological, nonoverlapping and inside the edited output."
                )
            previous = card.end_ms
        previous = 0
        for hold in self.visual_holds:
            if not previous <= hold.start_ms < hold.end_ms <= self.duration_ms:
                raise ValueError(
                    "Visual holds must be chronological, nonoverlapping and inside the edited output."
                )
            if not self.start_ms <= hold.source_ms < self.end_ms:
                raise ValueError(
                    "Held frames must come from the selected source interval."
                )
            previous = hold.end_ms
        if any(c.start_seconds >= self.duration_ms / 1000 for c in self.chapters):
            raise ValueError("A chapter extends beyond its clip.")
        return self


def _clock(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def chapters_text(chapters: list[dict]) -> str:
    return "\n".join(f"{_clock(c['start_seconds'])} {c['title']}" for c in chapters)
