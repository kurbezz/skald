from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class MediaType(str, Enum):
    MOVIE = "movie"
    TV = "tv"


class JobStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    ORGANIZING = "organizing"
    ORGANIZED = "organized"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MediaJob(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    type: MediaType
    title: str
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    release_title: str
    qbit_hash: str
    category: str
    status: JobStatus = Field(default=JobStatus.QUEUED)
    error_message: Optional[str] = None
    content_path: Optional[str] = None
    progress: float = Field(default=0.0)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
