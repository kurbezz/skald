from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import CheckConstraint, Column, JSON, UniqueConstraint
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
    lifecycle: FileLifecycle = Field(
        default=FileLifecycle.LEGACY_UNVERIFIED,
        sa_column_kwargs={"server_default": "LEGACY_UNVERIFIED"},
    )
    staging_path: Optional[str] = None
    staging_device: Optional[int] = None
    staging_inode: Optional[int] = None
    published_device: Optional[int] = None
    published_inode: Optional[int] = None


class MediaSubscription(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("tmdb_id", "type", name="uq_subscription_tmdb_type"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    tmdb_id: int = Field(index=True)
    type: MediaType
    title: str
    original_title: Optional[str] = None
    year: Optional[int] = None
    poster_url: Optional[str] = None
    is_active: bool = Field(default=True)
    auto_download: bool = Field(default=False)
    auto_grabbed_release_id: Optional[int] = Field(
        default=None, foreign_key="subscriptionrelease.id"
    )
    created_at: datetime = Field(default_factory=_utcnow)
    last_checked_at: Optional[datetime] = None
    next_check_at: datetime = Field(default_factory=_utcnow, index=True)
    last_error: Optional[str] = None


class TvSubscriptionScope(SQLModel, table=True):
    """A durable whole-series, season, or episode target for a TV subscription."""

    __table_args__ = (
        CheckConstraint(
            "(includes_future_content = 1 "
            "AND tmdb_season_id IS NULL AND tmdb_episode_id IS NULL "
            "AND season_number IS NULL AND episode_number IS NULL) "
            "OR (includes_future_content = 0 "
            "AND tmdb_season_id IS NOT NULL AND season_number IS NOT NULL "
            "AND ((tmdb_episode_id IS NULL AND episode_number IS NULL) "
            "OR (tmdb_episode_id IS NOT NULL AND episode_number IS NOT NULL)))",
            name="ck_tvsubscriptionscope_shape",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    subscription_id: int = Field(
        foreign_key="mediasubscription.id", ondelete="CASCADE", index=True
    )
    tmdb_series_id: int = Field(index=True)
    tmdb_season_id: Optional[int] = None
    tmdb_episode_id: Optional[int] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    includes_future_content: bool = Field(default=False)


class QualityProfile(SQLModel, table=True):
    """The one global release-quality policy, always stored under ID 1."""

    __table_args__ = (CheckConstraint("id = 1", name="ck_qualityprofile_singleton"),)

    id: int = Field(default=1, primary_key=True)
    allowed_resolutions: list[str] = Field(
        default_factory=lambda: ["1080p", "2160p"],
        sa_column=Column(JSON, nullable=False),
    )
    excluded_tokens: list[str] = Field(
        default_factory=lambda: ["CAM", "TS", "TeleSync"],
        sa_column=Column(JSON, nullable=False),
    )
    minimum_seeders: int = Field(default=5)
    updated_at: datetime = Field(default_factory=_utcnow)


class SubscriptionRelease(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    subscription_id: int = Field(foreign_key="mediasubscription.id", index=True)
    release_title: str
    indexer: str
    size_bytes: int
    seeders: int
    leechers: int
    download_url: str
    published_at: Optional[datetime] = None
    discovered_at: datetime = Field(default_factory=_utcnow, index=True)
    fingerprint: str = Field(index=True, sa_column_kwargs={"unique": True})
    read_at: Optional[datetime] = None


class SubscriptionReleaseScope(SQLModel, table=True):
    """The TV targets matched by a discovered release, retained for later grabbing."""

    __table_args__ = (
        UniqueConstraint(
            "subscription_release_id",
            "tv_subscription_scope_id",
            name="uq_subscription_release_scope",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    subscription_release_id: int = Field(
        foreign_key="subscriptionrelease.id", ondelete="CASCADE", index=True
    )
    tv_subscription_scope_id: int = Field(
        foreign_key="tvsubscriptionscope.id", ondelete="CASCADE", index=True
    )
