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
    DELETING = "deleting"
    FAILED = "failed"


class OrganizationMode(str, Enum):
    SCALAR = "scalar"
    PACK = "pack"


class FileLifecycle(str, Enum):
    RESERVED = "reserved"
    STAGED = "staged"
    PUBLISHED = "published"
    DELETE_REQUESTED = "delete_requested"
    LEGACY_UNVERIFIED = "legacy_unverified"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MediaJob(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    type: MediaType
    title: str
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_set: Optional[str] = None
    release_title: str
    qbit_hash: str
    category: str
    status: JobStatus = Field(default=JobStatus.QUEUED)
    error_message: Optional[str] = None
    content_path: Optional[str] = None
    library_path: Optional[str] = None
    organization_mode: OrganizationMode = Field(
        default=OrganizationMode.SCALAR,
        sa_column_kwargs={"server_default": "SCALAR"},
    )
    operation_token: Optional[str] = None
    progress: float = Field(default=0.0)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class OrganizedFile(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="mediajob.id", index=True)
    path: str = Field(sa_column_kwargs={"unique": True})
    operation_token: Optional[str] = None
    lifecycle: FileLifecycle
    staging_path: Optional[str] = None
    staging_device: Optional[int] = None
    staging_inode: Optional[int] = None
    published_device: Optional[int] = None
    published_inode: Optional[int] = None
