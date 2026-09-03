import asyncio
import errno
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from skald.db import get_engine, migrate_schema
from skald.models import (
    FileLifecycle,
    JobStatus,
    MediaJob,
    MediaType,
    OrganizationMode,
    OrganizedFile,
)
from skald.qbittorrent import TorrentStatus
from skald.worker import (
    DeletionOutcome,
    poll_once,
    reconcile_deleting_job,
    reconcile_organizing_job,
    request_job_deletion,
    worker_loop,
)


class FakeQbit:
    def __init__(self, statuses: dict[str, TorrentStatus]):
        self.statuses = statuses

    def get_status(self, torrent_hash: str) -> TorrentStatus:
        return self.statuses[torrent_hash]

    def delete_torrent(self, torrent_hash: str, delete_files: bool = True) -> None:
        return None


def make_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return engine


async def test_poll_once_marks_downloading(tmp_path):
    engine = make_engine()
    with Session(engine) as session:
        session.add(MediaJob(
            type=MediaType.MOVIE, title="The Matrix", year=1999,
            release_title="The.Matrix.1999", qbit_hash="hash1",
            category="skald-movie", status=JobStatus.QUEUED,
        ))
        session.commit()

    qbit = FakeQbit({
        "hash1": TorrentStatus(hash="hash1", progress=0.5, state="downloading",
                                content_path=str(tmp_path), save_path=str(tmp_path)),
    })

    with Session(engine) as session:
        await poll_once(session, qbit, movies_root=str(tmp_path / "movies"),
                        tv_root=str(tmp_path / "tv"))

    with Session(engine) as session:
        refreshed = session.exec(select(MediaJob)).first()
        assert refreshed.status == JobStatus.DOWNLOADING
        assert refreshed.progress == 0.5


async def test_poll_once_does_not_overwrite_job_deleted_during_status_fetch(tmp_path):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Movie"
    content_dir.mkdir(parents=True)
    (content_dir / "movie.mkv").write_text("data")
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.MOVIE, title="Movie", year=2020,
            release_title="Movie.2020", qbit_hash="hash", category="skald-movie",
            status=JobStatus.DOWNLOADING,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    class DeletesDuringStatusFetch:
        def get_status(self, torrent_hash):
            with Session(engine) as concurrent_session:
                concurrent_job = concurrent_session.get(MediaJob, job_id)
                concurrent_job.status = JobStatus.DELETING
                concurrent_session.add(concurrent_job)
                concurrent_session.commit()
            return TorrentStatus(
                hash="hash", progress=1.0, state="uploading",
                content_path=str(content_dir), save_path=str(tmp_path / "downloads"),
            )

    with Session(engine) as session:
        await poll_once(session, DeletesDuringStatusFetch(), str(tmp_path / "movies"), str(tmp_path / "tv"))

    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.DELETING, job.error_message
        assert job.library_path is None


async def test_poll_once_does_not_fail_job_deleted_when_status_fetch_raises(tmp_path):
    engine = make_engine()
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.MOVIE, title="Movie", year=2020,
            release_title="Movie.2020", qbit_hash="hash", category="skald-movie",
            status=JobStatus.DOWNLOADING,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    class DeletesThenRaisesDuringStatusFetch:
        def get_status(self, torrent_hash):
            with Session(engine) as concurrent_session:
                concurrent_job = concurrent_session.get(MediaJob, job_id)
                concurrent_job.status = JobStatus.DELETING
                concurrent_session.add(concurrent_job)
                concurrent_session.commit()
            raise RuntimeError("qBittorrent unavailable")

    with Session(engine) as session:
        await poll_once(
            session, DeletesThenRaisesDuringStatusFetch(), str(tmp_path / "movies"), str(tmp_path / "tv")
        )

    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.DELETING
        assert job.error_message is None


async def test_worker_loop_retries_after_deleting_discovery_query_failure(monkeypatch, tmp_path):
    engine = make_engine()
    first_session = Session(engine)
    original_exec = first_session.exec
    exec_calls = 0
    rollbacks = 0

    def fail_deleting_discovery(statement, *args, **kwargs):
        nonlocal exec_calls
        exec_calls += 1
        if exec_calls == 3:
            raise RuntimeError("database is locked")
        return original_exec(statement, *args, **kwargs)

    original_rollback = first_session.rollback

    def record_rollback():
        nonlocal rollbacks
        rollbacks += 1
        original_rollback()

    monkeypatch.setattr(first_session, "exec", fail_deleting_discovery)
    monkeypatch.setattr(first_session, "rollback", record_rollback)
    session_factory_calls = 0

    def session_factory():
        nonlocal session_factory_calls
        session_factory_calls += 1
        return first_session if session_factory_calls == 1 else Session(engine)

    sleep_calls = 0

    async def stop_after_second_interval(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("skald.worker.asyncio.sleep", stop_after_second_interval)

    with pytest.raises(asyncio.CancelledError):
        await worker_loop(session_factory, FakeQbit({}), str(tmp_path / "movies"), str(tmp_path / "tv"), 1)

    assert rollbacks == 1
    assert session_factory_calls == 2


async def test_poll_once_completes_idempotent_deleting_job(tmp_path):
    """A durably DELETING pack job whose owned public file is already
    missing (a previous partial attempt already removed it) and whose
    torrent is already absent must complete idempotently: a missing path is
    already clean, per the deletion protocol, regardless of what the
    recorded identity would have been.
    """
    engine = make_engine()
    missing_path = tmp_path / "library" / "Show - S01E01.mkv"
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01",
            qbit_hash="already-absent", category="skald-tv", status=JobStatus.DELETING,
            organization_mode=OrganizationMode.PACK, operation_token="delete-token",
        )
        session.add(job)
        session.commit()
        session.add(OrganizedFile(
            job_id=job.id, path=str(missing_path), lifecycle=FileLifecycle.DELETE_REQUESTED,
            operation_token="delete-token", published_device=1, published_inode=1,
        ))
        session.commit()

    class AlreadyAbsentQbit:
        def delete_torrent(self, torrent_hash, delete_files=True):
            raise LookupError("torrent not found")

    with Session(engine) as session:
        await poll_once(session, AlreadyAbsentQbit(), str(tmp_path / "movies"), str(tmp_path / "tv"))

    with Session(engine) as session:
        assert session.exec(select(MediaJob)).all() == []
        assert session.exec(select(OrganizedFile)).all() == []


async def test_poll_once_organizes_completed_movie(tmp_path):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "The.Matrix.1999"
    content_dir.mkdir(parents=True)
    (content_dir / "movie.mkv").write_text("data")

    with Session(engine) as session:
        session.add(MediaJob(
            type=MediaType.MOVIE, title="The Matrix", year=1999,
            release_title="The.Matrix.1999", qbit_hash="hash1",
            category="skald-movie", status=JobStatus.DOWNLOADING,
        ))
        session.commit()

    qbit = FakeQbit({
        "hash1": TorrentStatus(hash="hash1", progress=1.0, state="uploading",
                                content_path=str(content_dir),
                                save_path=str(tmp_path / "downloads")),
    })
    movies_root = tmp_path / "library" / "movies"

    with Session(engine) as session:
        await poll_once(session, qbit, movies_root=str(movies_root),
                        tv_root=str(tmp_path / "tv"))

    with Session(engine) as session:
        refreshed = session.exec(select(MediaJob)).first()
        assert refreshed.status == JobStatus.ORGANIZED
        assert refreshed.library_path == str(
            movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
        )

    target = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert target.exists()


async def test_poll_once_organizes_completed_tv_episode(tmp_path):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Breaking.Bad.S01E05"
    content_dir.mkdir(parents=True)
    (content_dir / "episode.mkv").write_text("data")

    with Session(engine) as session:
        session.add(MediaJob(
            type=MediaType.TV, title="Breaking Bad", season=1, episode=5,
            release_title="Breaking.Bad.S01E05", qbit_hash="hash1",
            category="skald-tv", status=JobStatus.DOWNLOADING,
        ))
        session.commit()

    qbit = FakeQbit({
        "hash1": TorrentStatus(hash="hash1", progress=1.0, state="uploading",
                                content_path=str(content_dir),
                                save_path=str(tmp_path / "downloads")),
    })
    tv_root = tmp_path / "library" / "tv"

    with Session(engine) as session:
        await poll_once(session, qbit, movies_root=str(tmp_path / "movies"),
                        tv_root=str(tv_root))

    target = tv_root / "Breaking Bad" / "Season 01" / "Breaking Bad - S01E05.mkv"
    assert target.exists()

    with Session(engine) as session:
        refreshed = session.exec(select(MediaJob)).first()
        assert refreshed.library_path == str(target)


async def test_poll_once_organizes_single_video_multi_episode_tv_target(tmp_path):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Black.Mirror.S07E01-E06"
    content_dir.mkdir(parents=True)
    (content_dir / "episode.mkv").write_text("data")

    with Session(engine) as session:
        session.add(MediaJob(
            type=MediaType.TV, title="Black Mirror", season=7, episode=1,
            episode_set="[1,2,3,4,5,6]", release_title="Black.Mirror.S07E01-E06",
            qbit_hash="hash1", category="skald-tv", status=JobStatus.DOWNLOADING,
        ))
        session.commit()

    qbit = FakeQbit({
        "hash1": TorrentStatus(hash="hash1", progress=1.0, state="uploading",
                                content_path=str(content_dir),
                                save_path=str(tmp_path / "downloads")),
    })
    tv_root = tmp_path / "library" / "tv"

    with Session(engine) as session:
        await poll_once(session, qbit, movies_root=str(tmp_path / "movies"),
                        tv_root=str(tv_root))

    target = tv_root / "Black Mirror" / "Season 07" / "Black Mirror - S07E01-E06.mkv"
    assert target.exists()
    with Session(engine) as session:
        job = session.exec(select(MediaJob)).one()
        assert job.status == JobStatus.ORGANIZED
        assert job.organization_mode == OrganizationMode.SCALAR
        assert job.library_path == str(target)


async def test_poll_once_marks_single_video_tv_with_malformed_episode_set_needs_attention(tmp_path):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Black.Mirror.S07E01-E06"
    content_dir.mkdir(parents=True)
    (content_dir / "episode.mkv").write_text("data")

    with Session(engine) as session:
        session.add(MediaJob(
            type=MediaType.TV, title="Black Mirror", season=7, episode=1,
            episode_set="[1,0]", release_title="Black.Mirror.S07E01-E06",
            qbit_hash="hash1", category="skald-tv", status=JobStatus.DOWNLOADING,
        ))
        session.commit()

    qbit = FakeQbit({
        "hash1": TorrentStatus(hash="hash1", progress=1.0, state="uploading",
                                content_path=str(content_dir),
                                save_path=str(tmp_path / "downloads")),
    })

    for _ in range(2):
        with Session(engine) as session:
            await poll_once(session, qbit, str(tmp_path / "movies"), str(tmp_path / "tv"))

    with Session(engine) as session:
        job = session.exec(select(MediaJob)).one()
        assert job.status == JobStatus.NEEDS_ATTENTION
        assert "malformed episode_set" in job.error_message.lower()
        assert job.library_path is None


async def test_poll_once_organizes_all_episodes_in_tv_pack(tmp_path):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Show.S01"
    nested = content_dir / "season"
    nested.mkdir(parents=True)
    for episode in range(1, 3):
        (nested / f"Show.S01.E{episode:02d}.mkv").write_text(str(episode))
    (nested / "Show.srt").write_text("subtitle")

    with Session(engine) as session:
        session.add(MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01",
            episode_set="[1,2,3,4,5,6]", qbit_hash="hash", category="skald-tv",
            status=JobStatus.DOWNLOADING,
        ))
        session.commit()
    qbit = FakeQbit({
        "hash": TorrentStatus(
            hash="hash", progress=1.0, state="uploading",
            content_path=str(content_dir), save_path=str(tmp_path / "downloads"),
        ),
    })

    with Session(engine) as session:
        await poll_once(session, qbit, str(tmp_path / "movies"), str(tmp_path / "tv"))

    targets = [
        tmp_path / "tv" / "Show" / "Season 01" / f"Show - S01E{episode:02d}.mkv"
        for episode in range(1, 3)
    ]
    assert all(path.exists() for path in targets)
    with Session(engine) as session:
        job = session.exec(select(MediaJob)).one()
        assert job.status == JobStatus.ORGANIZED
        assert job.library_path == str(targets[0])
        assert {file.path for file in session.exec(select(OrganizedFile)).all()} == {
            str(path) for path in targets
        }


async def test_pack_fence_reservation_commits_before_staging_is_visible_to_other_sessions(
    tmp_path, monkeypatch
):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Show.S01"
    content_dir.mkdir(parents=True)
    for episode in range(1, 3):
        (content_dir / f"Show.S01.E{episode:02d}.mkv").write_text(str(episode))
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01",
            qbit_hash="hash", category="skald-tv", status=JobStatus.DOWNLOADING,
        )
        session.add(job)
        session.commit()
        job_id = job.id
    qbit = FakeQbit({
        "hash": TorrentStatus(
            hash="hash", progress=1.0, state="uploading",
            content_path=str(content_dir), save_path=str(tmp_path / "downloads"),
        ),
    })

    import skald.worker as worker_module
    original_stage_file = worker_module.stage_file
    observed = {}
    calls = 0

    def observe_reservation_from_distinct_session(source, staging_path):
        nonlocal calls
        calls += 1
        if calls == 1:
            with Session(engine) as verifier:
                prepared_job = verifier.get(MediaJob, job_id)
                rows = verifier.exec(
                    select(OrganizedFile).where(OrganizedFile.job_id == job_id)
                ).all()
            observed["job_status"] = prepared_job.status
            observed["mode"] = prepared_job.organization_mode
            observed["token"] = prepared_job.operation_token
            observed["library_path"] = prepared_job.library_path
            observed["row_lifecycles"] = {row.lifecycle for row in rows}
            observed["row_tokens"] = {row.operation_token for row in rows}
            observed["row_count"] = len(rows)
        return original_stage_file(source, staging_path)

    monkeypatch.setattr("skald.worker.stage_file", observe_reservation_from_distinct_session)
    with Session(engine) as session:
        await poll_once(session, qbit, str(tmp_path / "movies"), str(tmp_path / "tv"))

    assert observed["job_status"] == JobStatus.ORGANIZING
    assert observed["mode"] == OrganizationMode.PACK
    assert observed["library_path"] is None
    assert observed["token"] is not None
    assert observed["row_count"] == 2
    assert observed["row_lifecycles"] == {FileLifecycle.RESERVED}
    assert observed["row_tokens"] == {observed["token"]}


async def test_pack_fence_stale_token_stops_further_publication_and_finalize(
    tmp_path, monkeypatch
):
    """A job-token replacement that lands between the per-row precheck and the
    row's own conditional UPDATE must still block that row's transition at
    the SQL level (B1): the in-memory precheck alone is not a correctness
    fence, since it races with concurrent commits from another session.
    """
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Show.S01"
    content_dir.mkdir(parents=True)
    for episode in range(1, 3):
        (content_dir / f"Show.S01.E{episode:02d}.mkv").write_text(str(episode))
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01",
            qbit_hash="hash", category="skald-tv", status=JobStatus.DOWNLOADING,
        )
        session.add(job)
        session.commit()
        job_id = job.id
    qbit = FakeQbit({
        "hash": TorrentStatus(
            hash="hash", progress=1.0, state="uploading",
            content_path=str(content_dir), save_path=str(tmp_path / "downloads"),
        ),
    })
    target_one = tmp_path / "tv" / "Show" / "Season 01" / "Show - S01E01.mkv"
    target_two = tmp_path / "tv" / "Show" / "Season 01" / "Show - S01E02.mkv"

    import skald.worker as worker_module
    original_stage_file = worker_module.stage_file
    calls = 0

    def replace_job_token_after_first_staging(source, staging_path):
        # The replacement happens *after* organize_tv_pack's per-row
        # precheck already passed for this row, and *before* its
        # subsequent conditional UPDATE call - exactly the race window B1
        # closes at the SQL level.
        nonlocal calls
        calls += 1
        result = original_stage_file(source, staging_path)
        if calls == 1:
            with Session(engine) as racer:
                racing_job = racer.get(MediaJob, job_id)
                racing_job.operation_token = "racing-token"
                racer.add(racing_job)
                racer.commit()
        return result

    monkeypatch.setattr("skald.worker.stage_file", replace_job_token_after_first_staging)
    with Session(engine) as session:
        await poll_once(session, qbit, str(tmp_path / "movies"), str(tmp_path / "tv"))

    assert calls == 1
    # No public file must ever be visible: the first row's own conditional
    # UPDATE must have been blocked by the stale job token before staging
    # was ever published.
    assert not target_one.exists()
    assert not target_two.exists()
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status != JobStatus.ORGANIZED
        assert job.library_path is None
        assert job.operation_token == "racing-token"
        rows = {
            row.path: row
            for row in session.exec(
                select(OrganizedFile).where(OrganizedFile.job_id == job_id)
            ).all()
        }
        # Every row - including the one whose staging succeeded just before
        # the token race - must remain exactly where it started: RESERVED.
        assert rows[str(target_one)].lifecycle == FileLifecycle.RESERVED
        assert rows[str(target_one)].staging_device is None
        assert rows[str(target_one)].staging_inode is None
        assert rows[str(target_two)].lifecycle == FileLifecycle.RESERVED


async def test_conditional_finalize_zero_row_update_preserves_external_transition(
    tmp_path, monkeypatch
):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Show.S01"
    content_dir.mkdir(parents=True)
    for episode in range(1, 3):
        (content_dir / f"Show.S01.E{episode:02d}.mkv").write_text(str(episode))
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01",
            qbit_hash="hash", category="skald-tv", status=JobStatus.DOWNLOADING,
        )
        session.add(job)
        session.commit()
        job_id = job.id
    qbit = FakeQbit({
        "hash": TorrentStatus(
            hash="hash", progress=1.0, state="uploading",
            content_path=str(content_dir), save_path=str(tmp_path / "downloads"),
        ),
    })

    import skald.worker as worker_module
    original_publish_staged_file = worker_module.publish_staged_file
    calls = 0

    def replace_status_after_last_publish(staged, target):
        nonlocal calls
        calls += 1
        result = original_publish_staged_file(staged, target)
        if calls == 2:
            with Session(engine) as racer:
                racing_job = racer.get(MediaJob, job_id)
                racing_job.status = JobStatus.NEEDS_ATTENTION
                racing_job.error_message = "external transition"
                racer.add(racing_job)
                racer.commit()
        return result

    monkeypatch.setattr("skald.worker.publish_staged_file", replace_status_after_last_publish)
    with Session(engine) as session:
        await poll_once(session, qbit, str(tmp_path / "movies"), str(tmp_path / "tv"))

    assert calls == 2
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.NEEDS_ATTENTION
        assert job.error_message == "external transition"
        assert job.library_path is None
        rows = session.exec(
            select(OrganizedFile).where(OrganizedFile.job_id == job_id)
        ).all()
        assert all(row.lifecycle == FileLifecycle.PUBLISHED for row in rows)


async def test_scalar_compatibility_movie_organizes_without_ledger_or_staging(tmp_path):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "The.Matrix.1999"
    content_dir.mkdir(parents=True)
    (content_dir / "movie.mkv").write_text("data")

    with Session(engine) as session:
        session.add(MediaJob(
            type=MediaType.MOVIE, title="The Matrix", year=1999,
            release_title="The.Matrix.1999", qbit_hash="hash1",
            category="skald-movie", status=JobStatus.DOWNLOADING,
        ))
        session.commit()

    qbit = FakeQbit({
        "hash1": TorrentStatus(hash="hash1", progress=1.0, state="uploading",
                                content_path=str(content_dir),
                                save_path=str(tmp_path / "downloads")),
    })
    movies_root = tmp_path / "library" / "movies"

    with Session(engine) as session:
        await poll_once(session, qbit, movies_root=str(movies_root),
                        tv_root=str(tmp_path / "tv"))

    with Session(engine) as session:
        job = session.exec(select(MediaJob)).one()
        assert job.status == JobStatus.ORGANIZED
        assert job.organization_mode == OrganizationMode.SCALAR
        assert job.operation_token is None
        assert job.library_path == str(
            movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
        )
        assert session.exec(select(OrganizedFile)).all() == []

    target = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert target.exists()
    assert not (target.parent / ".skald-staging").exists()


async def test_scalar_compatibility_single_video_tv_organizes_without_ledger_or_staging(tmp_path):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Breaking.Bad.S01E05"
    content_dir.mkdir(parents=True)
    (content_dir / "episode.mkv").write_text("data")

    with Session(engine) as session:
        session.add(MediaJob(
            type=MediaType.TV, title="Breaking Bad", season=1, episode=5,
            release_title="Breaking.Bad.S01E05", qbit_hash="hash1",
            category="skald-tv", status=JobStatus.DOWNLOADING,
        ))
        session.commit()

    qbit = FakeQbit({
        "hash1": TorrentStatus(hash="hash1", progress=1.0, state="uploading",
                                content_path=str(content_dir),
                                save_path=str(tmp_path / "downloads")),
    })
    tv_root = tmp_path / "library" / "tv"

    with Session(engine) as session:
        await poll_once(session, qbit, movies_root=str(tmp_path / "movies"),
                        tv_root=str(tv_root))

    target = tv_root / "Breaking Bad" / "Season 01" / "Breaking Bad - S01E05.mkv"
    assert target.exists()

    with Session(engine) as session:
        job = session.exec(select(MediaJob)).one()
        assert job.organization_mode == OrganizationMode.SCALAR
        assert job.operation_token is None
        assert job.library_path == str(target)
        assert session.exec(select(OrganizedFile)).all() == []


async def test_organizing_recovery_retains_legacy_unverified_residual_and_marks_needs_attention(
    tmp_path,
):
    """A `legacy_unverified` ledger row has no trustworthy creation identity.
    Recovery must never auto-delete its public path or silently drop its
    row: it stays exactly as-is, and the job becomes an actionable
    `NEEDS_ATTENTION` for operator reconciliation.
    """
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Show.S01"
    content_dir.mkdir(parents=True)
    for episode in range(1, 3):
        (content_dir / f"Show.S01.E{episode:02d}.mkv").write_text(str(episode))
    residual = tmp_path / "tv" / "Show" / "Season 01" / "Show - S01E01.mkv"
    residual.parent.mkdir(parents=True)
    residual.write_text("partial")
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01",
            qbit_hash="hash", category="skald-tv", status=JobStatus.NEEDS_ATTENTION,
            content_path=str(content_dir), organization_mode=OrganizationMode.PACK,
            operation_token="stale-token",
        )
        session.add(job)
        session.commit()
        session.add(OrganizedFile(
            job_id=job.id, path=str(residual), lifecycle=FileLifecycle.LEGACY_UNVERIFIED
        ))
        session.commit()
        job.status = JobStatus.ORGANIZING
        session.add(job)
        session.commit()
        job_id = job.id
    qbit = FakeQbit({})

    with Session(engine) as session:
        await poll_once(session, qbit, str(tmp_path / "movies"), str(tmp_path / "tv"))

    assert residual.exists()
    assert residual.read_text() == "partial"
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.NEEDS_ATTENTION
        assert "ownership conflict" in job.error_message
        rows = session.exec(select(OrganizedFile)).all()
        assert [row.path for row in rows] == [str(residual)]
        assert rows[0].lifecycle == FileLifecycle.LEGACY_UNVERIFIED


def test_migrate_schema_rejects_duplicate_ledger_reservations(tmp_path):
    engine = get_engine(str(tmp_path / "duplicate-ledger.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE mediajob (id INTEGER PRIMARY KEY, library_path VARCHAR)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE organizedfile (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path VARCHAR NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO organizedfile (job_id, path) VALUES (1, '/library/Show/S01E01.mkv')"
        )
        connection.exec_driver_sql(
            "INSERT INTO organizedfile (job_id, path) VALUES (2, '/library/Show/S01E01.mkv')"
        )

    with pytest.raises(RuntimeError, match="duplicate ledger path reservations"):
        migrate_schema(engine)

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM organizedfile").scalar() == 2


async def test_migrate_schema_legacy_pack_database_is_discoverable_without_lookuperror(tmp_path):
    """P5-B1 regression: `migrate_schema` must write the uppercase Enum
    *member-name* encoding SQLModel's `Enum` column type expects for
    `organization_mode`/`lifecycle` (not the lowercase Python values). A
    legacy database upgraded with the wrong encoding raises `LookupError`
    the instant any migrated row is loaded through the ORM - which poisons
    `poll_once`'s discovery query and would silently stop the worker from
    doing *any* work, forever, on every poll (not just skip the migrated
    job). `active_jobs_payload` loads every job unconditionally and would
    break the same way, taking `/jobs` and its WebSocket down with it.
    """
    from skald.routes.jobs import active_jobs_payload

    engine = get_engine(str(tmp_path / "legacy-pack.db"))
    content_dir = tmp_path / "downloads" / "Show.S01"
    content_dir.mkdir(parents=True)
    with engine.begin() as connection:
        # A realistic pre-migration schema: a TV job with an existing pack
        # ledger (the "already organized under the old model" shape the
        # design's migration policy targets), no lifecycle columns yet.
        connection.exec_driver_sql(
            "CREATE TABLE mediajob ("
            "id INTEGER PRIMARY KEY, type VARCHAR NOT NULL, title VARCHAR NOT NULL, "
            "year INTEGER, season INTEGER, episode INTEGER, "
            "release_title VARCHAR NOT NULL, qbit_hash VARCHAR NOT NULL, "
            "category VARCHAR NOT NULL, status VARCHAR NOT NULL, "
            "error_message VARCHAR, content_path VARCHAR, library_path VARCHAR, "
            "progress FLOAT NOT NULL DEFAULT 0.0, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE organizedfile (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path VARCHAR NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO mediajob "
            "(id, type, title, release_title, qbit_hash, category, status, content_path, "
            "progress, created_at, updated_at) VALUES "
            f"(2, 'TV', 'Show', 'Show.S01', 'legacy-hash', 'skald-tv', 'COMPLETED', '{content_dir}', "
            "1.0, '2026-09-02T00:00:00', '2026-09-02T00:00:00')"
        )
        connection.exec_driver_sql(
            "INSERT INTO organizedfile (job_id, path) VALUES "
            "(2, '/library/tv/Show/Season 01/Show - S01E01.mkv'), "
            "(2, '/library/tv/Show/Season 01/Show - S01E02.mkv')"
        )

    migrate_schema(engine)

    qbit = FakeQbit({
        "legacy-hash": TorrentStatus(
            hash="legacy-hash", progress=1.0, state="uploading",
            content_path=str(content_dir), save_path=str(tmp_path / "downloads"),
        ),
    })

    with Session(engine) as session:
        # Must not raise LookupError, and must not be silently swallowed:
        # the migrated job needs to actually be discovered and processed.
        await poll_once(session, qbit, str(tmp_path / "movies"), str(tmp_path / "tv"))

    with Session(engine) as session:
        job = session.get(MediaJob, 2)
        assert job.organization_mode == OrganizationMode.PACK
        # No video files exist in the (empty) content dir, so organize_job
        # reports NEEDS_ATTENTION; the important assertion is that it moved
        # at all instead of being permanently stuck at COMPLETED because
        # discovery raised and poll_once returned early doing nothing.
        assert job.status == JobStatus.NEEDS_ATTENTION

    # Loads every job unconditionally regardless of status; must not raise.
    payload = active_jobs_payload(engine)
    assert payload["completed_count"] == 1


async def test_request_job_deletion_leaves_migrated_legacy_unverified_row_untouched(tmp_path):
    """P5-B1 regression: `request_job_deletion`'s SQL-level guard
    (`WHERE lifecycle != LEGACY_UNVERIFIED`) must genuinely *exclude*
    migrated legacy rows, not match them because of an encoding mismatch.
    A wrong (lowercase-vs-uppercase) encoding would invert this guard,
    silently re-tokening an untrusted legacy row to `delete_requested` -
    exactly what the design forbids.
    """
    engine = get_engine(str(tmp_path / "legacy-delete.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE mediajob ("
            "id INTEGER PRIMARY KEY, type VARCHAR NOT NULL, title VARCHAR NOT NULL, "
            "year INTEGER, season INTEGER, episode INTEGER, "
            "release_title VARCHAR NOT NULL, qbit_hash VARCHAR NOT NULL, "
            "category VARCHAR NOT NULL, status VARCHAR NOT NULL, "
            "error_message VARCHAR, content_path VARCHAR, library_path VARCHAR, "
            "progress FLOAT NOT NULL DEFAULT 0.0, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE organizedfile (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path VARCHAR NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO mediajob "
            "(id, type, title, release_title, qbit_hash, category, status, "
            "progress, created_at, updated_at) VALUES "
            "(2, 'TV', 'Show', 'Show.S01', 'legacy-hash', 'skald-tv', 'ORGANIZED', "
            "1.0, '2026-09-02T00:00:00', '2026-09-02T00:00:00')"
        )
        connection.exec_driver_sql(
            "INSERT INTO organizedfile (job_id, path) VALUES "
            "(2, '/library/tv/Show/Season 01/Show - S01E01.mkv')"
        )

    migrate_schema(engine)

    with Session(engine) as session:
        job = request_job_deletion(session, 2)
        assert job.status == JobStatus.DELETING
        assert job.operation_token is not None

    with Session(engine) as session:
        row = session.exec(
            select(OrganizedFile).where(OrganizedFile.job_id == 2)
        ).one()
        # The guard must have excluded this legacy row entirely: its
        # lifecycle and token stay exactly as migration left them, not
        # rewritten to delete_requested/the new delete token.
        assert row.lifecycle is FileLifecycle.LEGACY_UNVERIFIED
        assert row.operation_token is None


async def test_pack_fence_preparation_commit_failure_makes_no_filesystem_calls(tmp_path, monkeypatch):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Show.S01"
    content_dir.mkdir(parents=True)
    for episode in range(1, 3):
        (content_dir / f"Show.S01.E{episode:02d}.mkv").write_text(str(episode))
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01",
            qbit_hash="hash", category="skald-tv", status=JobStatus.DOWNLOADING,
        )
        session.add(job)
        session.commit()
        job_id = job.id
    qbit = FakeQbit({
        "hash": TorrentStatus(
            hash="hash", progress=1.0, state="uploading",
            content_path=str(content_dir), save_path=str(tmp_path / "downloads"),
        ),
    })
    filesystem_called = False

    def fail_if_called(source, staging_path):
        nonlocal filesystem_called
        filesystem_called = True
        raise AssertionError("filesystem write was attempted")

    with Session(engine) as session:
        original_commit = session.commit
        commit_calls = 0

        def fail_preparation_commit():
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 2:
                assert session.get(MediaJob, job_id).status == JobStatus.ORGANIZING
                assert len(session.exec(select(OrganizedFile)).all()) == 2
                raise SQLAlchemyError("ledger preparation failed")
            original_commit()

        monkeypatch.setattr("skald.worker.stage_file", fail_if_called)
        monkeypatch.setattr(session, "commit", fail_preparation_commit)
        await poll_once(session, qbit, str(tmp_path / "movies"), str(tmp_path / "tv"))

    assert not filesystem_called
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.NEEDS_ATTENTION
        assert "ledger preparation failed" in job.error_message


async def test_pack_fence_staging_failure_marks_needs_attention_without_finalizing(
    tmp_path, monkeypatch
):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Show.S01"
    content_dir.mkdir(parents=True)
    for episode in range(1, 3):
        (content_dir / f"Show.S01.E{episode:02d}.mkv").write_text(str(episode))
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01",
            qbit_hash="hash", category="skald-tv", status=JobStatus.DOWNLOADING,
        )
        session.add(job)
        session.commit()
        job_id = job.id
    qbit = FakeQbit({
        "hash": TorrentStatus(
            hash="hash", progress=1.0, state="uploading",
            content_path=str(content_dir), save_path=str(tmp_path / "downloads"),
        ),
    })

    import skald.worker as worker_module
    original_stage_file = worker_module.stage_file

    def fail_second_stage(source, staging_path):
        if staging_path.name.endswith("E02.mkv.part"):
            raise OSError("disk full")
        return original_stage_file(source, staging_path)

    monkeypatch.setattr("skald.worker.stage_file", fail_second_stage)
    with Session(engine) as session:
        await poll_once(session, qbit, str(tmp_path / "movies"), str(tmp_path / "tv"))

    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.NEEDS_ATTENTION
        assert "disk full" in job.error_message
        assert job.library_path is None


async def test_pack_fence_staging_failure_does_not_overwrite_external_deleting_status(
    tmp_path, monkeypatch
):
    """B2 regression: a staging/publish failure handled by
    _mark_pack_job_needs_attention must never clobber a durable DELETING
    status/token committed by another session (e.g. a delete request) that
    landed while this organize attempt was mid-flight.
    """
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Show.S01"
    content_dir.mkdir(parents=True)
    for episode in range(1, 3):
        (content_dir / f"Show.S01.E{episode:02d}.mkv").write_text(str(episode))
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01",
            qbit_hash="hash", category="skald-tv", status=JobStatus.DOWNLOADING,
        )
        session.add(job)
        session.commit()
        job_id = job.id
    qbit = FakeQbit({
        "hash": TorrentStatus(
            hash="hash", progress=1.0, state="uploading",
            content_path=str(content_dir), save_path=str(tmp_path / "downloads"),
        ),
    })

    import skald.worker as worker_module
    original_stage_file = worker_module.stage_file

    def fail_second_stage_after_external_delete(source, staging_path):
        if staging_path.name.endswith("E02.mkv.part"):
            # A delete request commits durable DELETING intent (with a
            # replaced operation token, per the delete protocol) between
            # this organize attempt's reservation and its staging failure.
            with Session(engine) as racer:
                racing_job = racer.get(MediaJob, job_id)
                racing_job.status = JobStatus.DELETING
                racing_job.operation_token = "delete-token"
                racer.add(racing_job)
                racer.commit()
            raise OSError("disk full")
        return original_stage_file(source, staging_path)

    monkeypatch.setattr(
        "skald.worker.stage_file", fail_second_stage_after_external_delete
    )
    with Session(engine) as session:
        await poll_once(session, qbit, str(tmp_path / "movies"), str(tmp_path / "tv"))

    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.DELETING
        assert job.operation_token == "delete-token"


async def test_pack_fence_publication_race_preserves_foreign_target_and_marks_needs_attention(
    tmp_path, monkeypatch
):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "Show.S01"
    content_dir.mkdir(parents=True)
    for episode in range(1, 3):
        (content_dir / f"Show.S01.E{episode:02d}.mkv").write_text(str(episode))
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01",
            qbit_hash="hash", category="skald-tv", status=JobStatus.DOWNLOADING,
        )
        session.add(job)
        session.commit()
        job_id = job.id
    qbit = FakeQbit({
        "hash": TorrentStatus(
            hash="hash", progress=1.0, state="uploading",
            content_path=str(content_dir), save_path=str(tmp_path / "downloads"),
        ),
    })
    foreign_target = tmp_path / "tv" / "Show" / "Season 01" / "Show - S01E02.mkv"

    import skald.worker as worker_module
    original_stage_file = worker_module.stage_file
    calls = 0

    def race_before_second_stage(source, staging_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            foreign_target.parent.mkdir(parents=True, exist_ok=True)
            foreign_target.write_text("foreign")
        return original_stage_file(source, staging_path)

    monkeypatch.setattr("skald.worker.stage_file", race_before_second_stage)
    with Session(engine) as session:
        await poll_once(session, qbit, str(tmp_path / "movies"), str(tmp_path / "tv"))

    assert foreign_target.read_text() == "foreign"
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.NEEDS_ATTENTION
        assert job.library_path is None
        rows = session.exec(
            select(OrganizedFile).where(OrganizedFile.job_id == job_id)
        ).all()
        assert len(rows) == 2


async def test_recovery_token_organizing_cleans_matching_published_identity_and_completes(
    tmp_path,
):
    """A crash-interrupted `ORGANIZING` pack job whose row is `published`
    under the job's *current* token, with a recorded identity that matches
    the file actually on disk, is safe to clean up automatically: the owned
    file is removed, the row is dropped, and the job returns to `COMPLETED`
    so the next poll retries organization from scratch.
    """
    engine = make_engine()
    target = tmp_path / "tv" / "Show" / "Season 01" / "Show - S01E01.mkv"
    target.parent.mkdir(parents=True)
    target.write_text("partial")
    from skald.lifecycle import file_identity
    identity = file_identity(target)
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01",
            qbit_hash="hash", category="skald-tv", status=JobStatus.ORGANIZING,
            organization_mode=OrganizationMode.PACK, operation_token="crash-token",
        )
        session.add(job)
        session.commit()
        session.add(OrganizedFile(
            job_id=job.id, path=str(target), lifecycle=FileLifecycle.PUBLISHED,
            operation_token="crash-token",
            published_device=identity.device, published_inode=identity.inode,
        ))
        session.commit()
        job_id = job.id

    with Session(engine) as session:
        await poll_once(session, FakeQbit({}), str(tmp_path / "movies"), str(tmp_path / "tv"))

    assert not target.exists()
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.COMPLETED
        assert session.exec(select(OrganizedFile)).all() == []


async def test_recovery_token_organizing_leaves_mismatched_token_row_untouched(tmp_path):
    """A row whose `operation_token` does not match the job's current token
    (e.g. a stale reservation from a superseded attempt) must never be
    cleaned automatically, even if its recorded identity would otherwise
    match the file on disk.
    """
    engine = make_engine()
    target = tmp_path / "tv" / "Show" / "Season 01" / "Show - S01E01.mkv"
    target.parent.mkdir(parents=True)
    target.write_text("owned by a stale attempt")
    from skald.lifecycle import file_identity
    identity = file_identity(target)
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01",
            qbit_hash="hash", category="skald-tv", status=JobStatus.ORGANIZING,
            organization_mode=OrganizationMode.PACK, operation_token="current-token",
        )
        session.add(job)
        session.commit()
        session.add(OrganizedFile(
            job_id=job.id, path=str(target), lifecycle=FileLifecycle.PUBLISHED,
            operation_token="stale-token",
            published_device=identity.device, published_inode=identity.inode,
        ))
        session.commit()
        job_id = job.id

    with Session(engine) as session:
        await poll_once(session, FakeQbit({}), str(tmp_path / "movies"), str(tmp_path / "tv"))

    assert target.exists()
    assert target.read_text() == "owned by a stale attempt"
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.NEEDS_ATTENTION
        rows = session.exec(select(OrganizedFile)).all()
        assert [row.path for row in rows] == [str(target)]
        assert rows[0].operation_token == "stale-token"


async def test_poll_once_needs_attention_when_no_video_files(tmp_path):
    engine = make_engine()
    content_dir = tmp_path / "downloads" / "empty"
    content_dir.mkdir(parents=True)

    with Session(engine) as session:
        session.add(MediaJob(
            type=MediaType.MOVIE, title="The Matrix", year=1999,
            release_title="The.Matrix.1999", qbit_hash="hash1",
            category="skald-movie", status=JobStatus.DOWNLOADING,
        ))
        session.commit()

    qbit = FakeQbit({
        "hash1": TorrentStatus(hash="hash1", progress=1.0, state="uploading",
                                content_path=str(content_dir),
                                save_path=str(tmp_path / "downloads")),
    })

    with Session(engine) as session:
        await poll_once(session, qbit, movies_root=str(tmp_path / "movies"),
                        tv_root=str(tmp_path / "tv"))

    with Session(engine) as session:
        refreshed = session.exec(select(MediaJob)).first()
        assert refreshed.status == JobStatus.NEEDS_ATTENTION
        assert "no video files" in refreshed.error_message.lower()


async def test_poll_once_isolates_failures_per_job(tmp_path):
    engine = make_engine()
    with Session(engine) as session:
        session.add(MediaJob(
            type=MediaType.MOVIE, title="Broken", year=2000,
            release_title="Broken.2000", qbit_hash="missing-hash",
            category="skald-movie", status=JobStatus.QUEUED,
        ))
        session.commit()

    qbit = FakeQbit({})  # get_status will raise KeyError for "missing-hash"

    with Session(engine) as session:
        await poll_once(session, qbit, movies_root=str(tmp_path / "movies"),
                        tv_root=str(tmp_path / "tv"))

    with Session(engine) as session:
        refreshed = session.exec(select(MediaJob)).first()
        assert refreshed.status == JobStatus.FAILED
        assert refreshed.error_message


# --- Phase 4: fenced deletion and recovery -------------------------------


def _make_pack_job(session, *, status=JobStatus.ORGANIZED, token="org-token"):
    job = MediaJob(
        type=MediaType.TV, title="Show", release_title="Show.S01",
        qbit_hash="hash", category="skald-tv", status=status,
        organization_mode=OrganizationMode.PACK, operation_token=token,
    )
    session.add(job)
    session.commit()
    return job


def _add_published_row(session, job, path, *, token="org-token"):
    from skald.lifecycle import file_identity
    identity = file_identity(path)
    row = OrganizedFile(
        job_id=job.id, path=str(path), lifecycle=FileLifecycle.PUBLISHED,
        operation_token=token, published_device=identity.device,
        published_inode=identity.inode,
    )
    session.add(row)
    session.commit()
    return row


async def test_delete_identity_removes_public_file_when_publish_succeeded_but_row_stuck_at_staged(
    tmp_path,
):
    """P4-B1 regression: simulate the exact crash/fence-loss window between
    `publish_staged_file` succeeding (hardlink to the public target, then
    unlink of the private staging file) and the `STAGED -> PUBLISHED`
    database write committing. The ledger row is left at `staged` with only
    a staging identity recorded, but the real public file already exists at
    `row.path` and, by the hardlink invariant asserted in
    `publish_staged_file`, carries exactly that staging identity. Deletion
    must find and remove it via `row.path`, not silently orphan it because
    `staging_path` is already (correctly) gone.

    Uses a real `stage_file` + `publish_staged_file` call sequence so the
    shared-inode hardlink invariant is genuinely exercised, not assumed.
    """
    from skald.organizer import publish_staged_file, stage_file, staging_path_for

    engine = make_engine()
    source = tmp_path / "downloads" / "Show.S01E01.mkv"
    source.parent.mkdir(parents=True)
    source.write_text("episode data")
    target = tmp_path / "library" / "Show - S01E01.mkv"
    token = "organize-token"
    staging_path = staging_path_for(target, token)

    staged = stage_file(source, staging_path)
    published = publish_staged_file(staged, target)
    # The real filesystem work is now done exactly as it would be
    # mid-organize: target exists, hardlinked from (now-unlinked) staging,
    # sharing its inode. Simulate the crash: never perform the
    # STAGED -> PUBLISHED database transition below.
    assert target.exists()
    assert not staging_path.exists()
    assert published.identity == staged.identity

    with Session(engine) as session:
        job = _make_pack_job(session, token=token)
        session.add(OrganizedFile(
            job_id=job.id, path=str(target), lifecycle=FileLifecycle.STAGED,
            operation_token=token, staging_path=str(staging_path),
            staging_device=staged.identity.device, staging_inode=staged.identity.inode,
        ))
        session.commit()
        job_id = job.id

    with Session(engine) as session:
        job = request_job_deletion(session, job_id)
        outcome = reconcile_deleting_job(session, job, FakeQbit({}))

    assert outcome == DeletionOutcome.DELETED
    assert not target.exists(), "public file was silently orphaned instead of removed"
    with Session(engine) as session:
        assert session.get(MediaJob, job_id) is None
        assert session.exec(select(OrganizedFile)).all() == []


async def test_recovery_token_organizing_removes_public_file_when_publish_succeeded_but_row_stuck_at_staged(
    tmp_path,
):
    """Same P4-B1 crash window as the deletion regression above, but for
    `reconcile_organizing_job`'s `STAGED` branch.
    """
    from skald.organizer import publish_staged_file, stage_file, staging_path_for

    engine = make_engine()
    source = tmp_path / "downloads" / "Show.S01E01.mkv"
    source.parent.mkdir(parents=True)
    source.write_text("episode data")
    target = tmp_path / "library" / "Show" / "Season 01" / "Show - S01E01.mkv"
    token = "crash-token"
    staging_path = staging_path_for(target, token)

    staged = stage_file(source, staging_path)
    publish_staged_file(staged, target)
    assert target.exists()
    assert not staging_path.exists()

    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01",
            qbit_hash="hash", category="skald-tv", status=JobStatus.ORGANIZING,
            organization_mode=OrganizationMode.PACK, operation_token=token,
        )
        session.add(job)
        session.commit()
        session.add(OrganizedFile(
            job_id=job.id, path=str(target), lifecycle=FileLifecycle.STAGED,
            operation_token=token, staging_path=str(staging_path),
            staging_device=staged.identity.device, staging_inode=staged.identity.inode,
        ))
        session.commit()
        job_id = job.id

    with Session(engine) as session:
        await poll_once(session, FakeQbit({}), str(tmp_path / "movies"), str(tmp_path / "tv"))

    assert not target.exists(), "public file was silently orphaned instead of removed"
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.COMPLETED
        assert session.exec(select(OrganizedFile)).all() == []


async def test_fenced_delete_commits_deleting_token_and_delete_requested_rows_before_any_io(
    tmp_path,
):
    engine = make_engine()
    published = tmp_path / "library" / "Show - S01E01.mkv"
    published.parent.mkdir(parents=True)
    published.write_text("data")
    with Session(engine) as session:
        job = _make_pack_job(session)
        _add_published_row(session, job, published)
        job_id = job.id
        old_token = job.operation_token

    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        committed = request_job_deletion(session, job_id)
        assert committed.status == JobStatus.DELETING
        assert committed.operation_token != old_token

    with Session(engine) as verifier:
        durable_job = verifier.get(MediaJob, job_id)
        assert durable_job.status == JobStatus.DELETING
        assert durable_job.operation_token == committed.operation_token
        rows = verifier.exec(
            select(OrganizedFile).where(OrganizedFile.job_id == job_id)
        ).all()
        assert len(rows) == 1
        assert rows[0].lifecycle == FileLifecycle.DELETE_REQUESTED
        assert rows[0].operation_token == committed.operation_token
    # No filesystem side effect has happened yet: the commit is the entire
    # observable effect of request_job_deletion.
    assert published.exists()


async def test_fenced_delete_reservation_commit_is_visible_before_first_cleanup_call(
    tmp_path, monkeypatch
):
    engine = make_engine()
    published = tmp_path / "library" / "Show - S01E01.mkv"
    published.parent.mkdir(parents=True)
    published.write_text("data")
    with Session(engine) as session:
        job = _make_pack_job(session)
        _add_published_row(session, job, published)
        job_id = job.id

    observed = {}
    import skald.worker as worker_module
    original_cleanup = worker_module.cleanup_owned_file

    def observe_from_distinct_session(path, identity):
        with Session(engine) as verifier:
            durable_job = verifier.get(MediaJob, job_id)
            rows = verifier.exec(
                select(OrganizedFile).where(OrganizedFile.job_id == job_id)
            ).all()
        observed["status"] = durable_job.status
        observed["token"] = durable_job.operation_token
        observed["row_lifecycles"] = {row.lifecycle for row in rows}
        return original_cleanup(path, identity)

    monkeypatch.setattr("skald.worker.cleanup_owned_file", observe_from_distinct_session)

    with Session(engine) as session:
        job = request_job_deletion(session, job_id)
        reconcile_deleting_job(session, job, FakeQbit({}))

    assert observed["status"] == JobStatus.DELETING
    assert observed["token"] is not None
    assert observed["row_lifecycles"] == {FileLifecycle.DELETE_REQUESTED}


async def test_delete_identity_matching_published_row_removed_missing_path_is_clean(
    tmp_path,
):
    engine = make_engine()
    present = tmp_path / "library" / "Show - S01E01.mkv"
    present.parent.mkdir(parents=True)
    present.write_text("data")
    already_gone = tmp_path / "library" / "Show - S01E02.mkv"
    with Session(engine) as session:
        job = _make_pack_job(session)
        _add_published_row(session, job, present)
        row2 = OrganizedFile(
            job_id=job.id, path=str(already_gone), lifecycle=FileLifecycle.PUBLISHED,
            operation_token="org-token", published_device=1, published_inode=1,
        )
        session.add(row2)
        session.commit()
        job_id = job.id

    with Session(engine) as session:
        job = request_job_deletion(session, job_id)
        outcome = reconcile_deleting_job(session, job, FakeQbit({}))

    assert outcome == DeletionOutcome.DELETED
    assert not present.exists()
    with Session(engine) as session:
        assert session.get(MediaJob, job_id) is None
        assert session.exec(select(OrganizedFile)).all() == []


async def test_delete_identity_full_pack_deletion_prunes_empty_season_directory(tmp_path):
    """Regression against shipped baseline (commit `595bed2`): scalar
    deletion prunes an organized file's now-empty parent directory via
    `remove_organized_file`; pack deletion's identity-checked cleanup must
    do the same, or empty `Show/Season 01/` trees accumulate in the library
    after every episode of a season is deleted.
    """
    engine = make_engine()
    season_dir = tmp_path / "tv" / "Show" / "Season 01"
    episodes = [season_dir / "Show - S01E01.mkv", season_dir / "Show - S01E02.mkv"]
    season_dir.mkdir(parents=True)
    for path in episodes:
        path.write_text("data")
    with Session(engine) as session:
        job = _make_pack_job(session)
        for path in episodes:
            _add_published_row(session, job, path)
        job_id = job.id

    with Session(engine) as session:
        job = request_job_deletion(session, job_id)
        outcome = reconcile_deleting_job(session, job, FakeQbit({}))

    assert outcome == DeletionOutcome.DELETED
    assert not any(path.exists() for path in episodes)
    # The now-empty Season 01 directory must be pruned, exactly like
    # scalar deletion prunes an organized file's empty parent.
    assert not season_dir.exists()
    # Single-level pruning only (matching remove_organized_file's existing
    # boundary logic): the show-level directory is left alone even if it is
    # now empty too.
    assert season_dir.parent.exists()


async def test_delete_identity_mismatch_and_legacy_row_are_retained_without_deleting_foreign_path(
    tmp_path,
):
    engine = make_engine()
    foreign = tmp_path / "library" / "Show - S01E01.mkv"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("owned by someone else now")
    legacy_path = tmp_path / "library" / "Show - S01E02.mkv"
    legacy_path.write_text("legacy content")
    with Session(engine) as session:
        job = _make_pack_job(session)
        # Recorded identity that does not match what's actually on disk.
        session.add(OrganizedFile(
            job_id=job.id, path=str(foreign), lifecycle=FileLifecycle.PUBLISHED,
            operation_token="org-token", published_device=999999, published_inode=999999,
        ))
        session.add(OrganizedFile(
            job_id=job.id, path=str(legacy_path), lifecycle=FileLifecycle.LEGACY_UNVERIFIED,
        ))
        session.commit()
        job_id = job.id

    qbit_calls = []

    class RecordingQbit:
        def delete_torrent(self, torrent_hash, delete_files=True):
            qbit_calls.append(torrent_hash)

    with Session(engine) as session:
        job = request_job_deletion(session, job_id)
        outcome = reconcile_deleting_job(session, job, RecordingQbit())

    assert outcome == DeletionOutcome.NEEDS_ATTENTION
    assert qbit_calls == []
    assert foreign.exists()
    assert foreign.read_text() == "owned by someone else now"
    assert legacy_path.exists()
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.NEEDS_ATTENTION
        assert "ownership conflict" in job.error_message
        rows = session.exec(select(OrganizedFile)).all()
        assert {row.path for row in rows} == {str(foreign), str(legacy_path)}


async def test_deleting_foreign_public_conflict_cleans_owned_staging_and_reports_both_paths(tmp_path):
    engine = make_engine()
    public_path = tmp_path / "library" / "Show - S01E01.mkv"
    staging_path = tmp_path / "library" / ".skald-staging" / "org-token" / "Show.part"
    public_path.parent.mkdir(parents=True)
    public_path.write_text("foreign public file")
    staging_path.parent.mkdir(parents=True)
    staging_path.write_text("owned private staging file")
    from skald.lifecycle import file_identity
    staging_identity = file_identity(staging_path)
    with Session(engine) as session:
        job = _make_pack_job(session)
        row = OrganizedFile(
            job_id=job.id,
            path=str(public_path),
            lifecycle=FileLifecycle.PUBLISHED,
            operation_token="org-token",
            published_device=999999,
            published_inode=999999,
            staging_path=str(staging_path),
            staging_device=staging_identity.device,
            staging_inode=staging_identity.inode,
        )
        session.add(row)
        session.commit()
        job_id = job.id
        row_id = row.id

    with Session(engine) as session:
        job = request_job_deletion(session, job_id)
        outcome = reconcile_deleting_job(session, job, FakeQbit({}))

    assert outcome == DeletionOutcome.NEEDS_ATTENTION
    assert public_path.exists()
    assert not staging_path.exists()
    assert not staging_path.parent.exists()
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.NEEDS_ATTENTION
        assert str(public_path) in job.error_message
        assert str(staging_path) in job.error_message
        assert session.get(OrganizedFile, row_id) is not None


async def test_organizing_foreign_public_conflict_cleans_owned_staging_and_reports_both_paths(tmp_path):
    engine = make_engine()
    public_path = tmp_path / "library" / "Show - S01E01.mkv"
    staging_path = tmp_path / "library" / ".skald-staging" / "org-token" / "Show.part"
    public_path.parent.mkdir(parents=True)
    public_path.write_text("foreign public file")
    staging_path.parent.mkdir(parents=True)
    staging_path.write_text("owned private staging file")
    from skald.lifecycle import file_identity
    staging_identity = file_identity(staging_path)
    with Session(engine) as session:
        job = _make_pack_job(session, status=JobStatus.ORGANIZING)
        row = OrganizedFile(
            job_id=job.id,
            path=str(public_path),
            lifecycle=FileLifecycle.PUBLISHED,
            operation_token="org-token",
            published_device=999999,
            published_inode=999999,
            staging_path=str(staging_path),
            staging_device=staging_identity.device,
            staging_inode=staging_identity.inode,
        )
        session.add(row)
        session.commit()
        job_id = job.id
        row_id = row.id

    with Session(engine) as session:
        reconcile_organizing_job(session, session.get(MediaJob, job_id))

    assert public_path.exists()
    assert not staging_path.exists()
    assert not staging_path.parent.exists()
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.NEEDS_ATTENTION
        assert str(public_path) in job.error_message
        assert str(staging_path) in job.error_message
        assert session.get(OrganizedFile, row_id) is not None


async def test_staging_failure_persists_owned_identity_for_organizing_recovery(tmp_path, monkeypatch):
    from skald.lifecycle import file_identity
    from skald.organizer import FileOperationError, FileOperationOutcome, OwnedPathIdentity
    from skald.worker import organize_tv_pack

    engine = make_engine()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    sources = [downloads / f"Show.S01E{episode:02d}.mkv" for episode in (1, 2)]
    for source in sources:
        source.write_text("episode")
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV,
            title="Show",
            release_title="Show.S01",
            qbit_hash="hash",
            category="skald-tv",
            status=JobStatus.COMPLETED,
        )
        session.add(job)
        session.commit()
        job_id = job.id

        def leave_owned_staging_file(source, staging_path):
            staging_path.parent.mkdir(parents=True)
            staging_path.write_text("partial")
            identity = file_identity(staging_path)
            raise FileOperationError(
                OSError(errno.ENOSPC, "disk full", str(staging_path)),
                FileOperationOutcome(
                    attempt_owned_paths=(staging_path,),
                    attempt_owned_identities=(OwnedPathIdentity(staging_path, identity),),
                ),
            )

        monkeypatch.setattr("skald.worker.stage_file", leave_owned_staging_file)
        organize_tv_pack(session, job, str(tmp_path / "tv"), sources)

    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.NEEDS_ATTENTION
        rows = session.exec(select(OrganizedFile).where(OrganizedFile.job_id == job_id)).all()
        owned_row = next(row for row in rows if row.staging_device is not None)
        staging_path = Path(owned_row.staging_path)
        staging_identity = file_identity(staging_path)
        assert (owned_row.staging_device, owned_row.staging_inode) == (
            staging_identity.device,
            staging_identity.inode,
        )
        job.status = JobStatus.ORGANIZING
        session.add(job)
        session.commit()

    with Session(engine) as session:
        reconcile_organizing_job(session, session.get(MediaJob, job_id))

    assert not staging_path.exists()
    assert not staging_path.parent.exists()
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.COMPLETED
        assert session.exec(select(OrganizedFile).where(OrganizedFile.job_id == job_id)).all() == []


async def test_deleting_recovery_qbittorrent_failure_after_local_cleanup_retains_ledger_for_retry(
    tmp_path,
):
    engine = make_engine()
    published = tmp_path / "library" / "Show - S01E01.mkv"
    published.parent.mkdir(parents=True)
    published.write_text("data")
    with Session(engine) as session:
        job = _make_pack_job(session)
        _add_published_row(session, job, published)
        job_id = job.id

    class FailingQbit:
        def delete_torrent(self, torrent_hash, delete_files=True):
            raise RuntimeError("qbittorrent unreachable")

    with Session(engine) as session:
        job = request_job_deletion(session, job_id)
        delete_token = job.operation_token
        outcome = reconcile_deleting_job(session, job, FailingQbit())

    assert outcome == DeletionOutcome.QBIT_FAILURE
    # The owned local file was already removed before qBittorrent was ever
    # attempted; only the durable retry intent and ledger remain.
    assert not published.exists()
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.DELETING
        assert job.operation_token == delete_token
        assert "Delete pending:" in job.error_message
        rows = session.exec(select(OrganizedFile)).all()
        assert [row.path for row in rows] == [str(published)]

    # Retry: the file is now already gone, so this attempt completes cleanly.
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        outcome = reconcile_deleting_job(session, job, FakeQbit({}))
    assert outcome == DeletionOutcome.DELETED
    with Session(engine) as session:
        assert session.get(MediaJob, job_id) is None


async def test_deleting_recovery_already_absent_torrent_is_treated_as_success(tmp_path):
    engine = make_engine()
    published = tmp_path / "library" / "Show - S01E01.mkv"
    published.parent.mkdir(parents=True)
    published.write_text("data")
    with Session(engine) as session:
        job = _make_pack_job(session)
        _add_published_row(session, job, published)
        job_id = job.id

    class AlreadyAbsentQbit:
        def delete_torrent(self, torrent_hash, delete_files=True):
            raise LookupError("torrent not found")

    with Session(engine) as session:
        job = request_job_deletion(session, job_id)
        outcome = reconcile_deleting_job(session, job, AlreadyAbsentQbit())

    assert outcome == DeletionOutcome.DELETED
    with Session(engine) as session:
        assert session.get(MediaJob, job_id) is None
        assert session.exec(select(OrganizedFile)).all() == []


async def test_recovery_token_deleting_row_with_stale_token_is_retained_as_ownership_conflict(
    tmp_path,
):
    """A ledger row whose token was never advanced to the current delete
    token (e.g. it belongs to a superseded operation) must never be treated
    as owned by this delete, even if a file happens to exist at its path.
    """
    engine = make_engine()
    path = tmp_path / "library" / "Show - S01E01.mkv"
    path.parent.mkdir(parents=True)
    path.write_text("data")
    with Session(engine) as session:
        job = _make_pack_job(session, status=JobStatus.DELETING, token="current-delete-token")
        from skald.lifecycle import file_identity
        identity = file_identity(path)
        session.add(OrganizedFile(
            job_id=job.id, path=str(path), lifecycle=FileLifecycle.DELETE_REQUESTED,
            operation_token="superseded-token",
            published_device=identity.device, published_inode=identity.inode,
        ))
        session.commit()
        job_id = job.id

    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        outcome = reconcile_deleting_job(session, job, FakeQbit({}))

    assert outcome == DeletionOutcome.NEEDS_ATTENTION
    assert path.exists()
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.NEEDS_ATTENTION


async def test_scalar_delete_commits_deleting_intent_then_removes_library_path_then_qbit(
    tmp_path,
):
    engine = make_engine()
    library_file = tmp_path / "library" / "Movie.mkv"
    library_file.parent.mkdir(parents=True)
    library_file.write_text("data")
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.MOVIE, title="Movie", year=2020,
            release_title="Movie.2020", qbit_hash="hash", category="skald-movie",
            status=JobStatus.ORGANIZED, library_path=str(library_file),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    qbit_calls = []

    class RecordingQbit:
        def delete_torrent(self, torrent_hash, delete_files=True):
            qbit_calls.append(torrent_hash)

    with Session(engine) as session:
        job = request_job_deletion(session, job_id)
        assert job.status == JobStatus.DELETING
        assert job.organization_mode == OrganizationMode.SCALAR
        # Scalar deletion never adopts a pack operation token.
        outcome = reconcile_deleting_job(session, job, RecordingQbit())

    assert outcome == DeletionOutcome.DELETED
    assert not library_file.exists()
    assert qbit_calls == ["hash"]
    with Session(engine) as session:
        assert session.get(MediaJob, job_id) is None


async def test_scalar_delete_removes_unexpected_child_metadata_without_using_its_path(tmp_path):
    """Design: pack deletion never falls back to `library_path`, and scalar
    deletion does not use pack identity fields. A scalar job's filesystem
    deletion must act on `library_path` only, even if it has an unexpected
    OrganizedFile child. The child metadata still must be removed before its
    parent job can be deleted under the child FK.
    """
    # get_engine enables SQLite foreign-key enforcement, unlike make_engine's
    # raw in-memory connection.
    engine = get_engine(str(tmp_path / "scalar-child-fk.db"))
    SQLModel.metadata.create_all(engine)
    scalar_path = tmp_path / "library" / "Movie.mkv"
    unrelated_path = tmp_path / "library" / "Show - S01E01.mkv"
    scalar_path.parent.mkdir(parents=True)
    scalar_path.write_text("scalar data")
    unrelated_path.write_text("unrelated data")
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.MOVIE, title="Movie", year=2020,
            release_title="Movie.2020", qbit_hash="hash", category="skald-movie",
            status=JobStatus.ORGANIZED, library_path=str(scalar_path),
        )
        session.add(job)
        session.flush()
        unrelated_identity = unrelated_path.stat()
        ledger = OrganizedFile(
            job_id=job.id,
            path=str(unrelated_path),
            operation_token="stale-pack-token",
            lifecycle=FileLifecycle.PUBLISHED,
            published_device=unrelated_identity.st_dev,
            published_inode=unrelated_identity.st_ino,
        )
        session.add_all([job, ledger])
        session.commit()
        job_id = job.id
        ledger_id = ledger.id

    class FailingQbit:
        def delete_torrent(self, torrent_hash, delete_files=True):
            raise RuntimeError("qBittorrent unavailable")

    with Session(engine) as session:
        job = request_job_deletion(session, job_id)
        outcome = reconcile_deleting_job(session, job, FailingQbit())

    assert outcome == DeletionOutcome.QBIT_FAILURE
    assert not scalar_path.exists()
    assert unrelated_path.exists()
    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.DELETING
        assert session.get(OrganizedFile, ledger_id) is not None

    class RecordingQbit:
        def __init__(self):
            self.deleted_hashes = []

        def delete_torrent(self, torrent_hash, delete_files=True):
            self.deleted_hashes.append(torrent_hash)

    fake_qbit = RecordingQbit()

    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        outcome = reconcile_deleting_job(session, job, fake_qbit)

    assert outcome == DeletionOutcome.DELETED
    assert not scalar_path.exists()
    assert unrelated_path.exists()
    with Session(engine) as session:
        assert session.get(MediaJob, job_id) is None
        assert session.get(OrganizedFile, ledger_id) is None
    assert fake_qbit.deleted_hashes == ["hash"]


async def test_pack_delete_flushes_owned_ledger_before_parent_under_foreign_key_enforcement(
    tmp_path, monkeypatch
):
    """A successful pack delete must flush its child rows before deleting the
    parent when SQLite enforces the real OrganizedFile.job_id FK. A qBit
    failure, conversely, must retain the durable DELETING intent and ledger.
    """
    engine = get_engine(str(tmp_path / "pack-child-fk.db"))
    SQLModel.metadata.create_all(engine)
    successful_paths = [
        tmp_path / "library" / f"Show - S01E{episode:02d}.mkv"
        for episode in (1, 2)
    ]
    failed_path = tmp_path / "library" / "Other Show - S01E01.mkv"
    for path in [*successful_paths, failed_path]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name)

    with Session(engine) as session:
        def add_pack_job(qbit_hash, paths):
            job = MediaJob(
                type=MediaType.TV, title="Show", release_title="Show.S01",
                qbit_hash=qbit_hash, category="skald-tv", status=JobStatus.ORGANIZED,
                organization_mode=OrganizationMode.PACK, operation_token=f"organize-{qbit_hash}",
            )
            session.add(job)
            session.flush()
            rows = []
            for path in paths:
                identity = path.stat()
                rows.append(OrganizedFile(
                    job_id=job.id, path=str(path), lifecycle=FileLifecycle.PUBLISHED,
                    operation_token=job.operation_token,
                    published_device=identity.st_dev, published_inode=identity.st_ino,
                ))
            session.add_all(rows)
            session.commit()
            return job.id, [row.id for row in rows]

        successful_job_id, successful_ledger_ids = add_pack_job("successful", successful_paths)
        failed_job_id, failed_ledger_ids = add_pack_job("failed", [failed_path])

    class FailingQbit:
        def delete_torrent(self, torrent_hash, delete_files=True):
            raise RuntimeError("qBittorrent unavailable")

    with Session(engine) as session:
        failed_job = request_job_deletion(session, failed_job_id)
        outcome = reconcile_deleting_job(session, failed_job, FailingQbit())

    assert outcome == DeletionOutcome.QBIT_FAILURE
    with Session(engine) as session:
        assert session.get(MediaJob, failed_job_id).status == JobStatus.DELETING
        assert all(session.get(OrganizedFile, row_id) is not None for row_id in failed_ledger_ids)

    class RecordingQbit:
        def __init__(self):
            self.deleted_hashes = []

        def delete_torrent(self, torrent_hash, delete_files=True):
            self.deleted_hashes.append(torrent_hash)

    fake_qbit = RecordingQbit()
    with Session(engine) as session:
        successful_job = request_job_deletion(session, successful_job_id)
        parent_scheduled = False
        flushes_before_parent = []
        original_delete = session.delete
        original_flush = session.flush

        def record_delete(instance):
            nonlocal parent_scheduled
            if instance is successful_job:
                parent_scheduled = True
            return original_delete(instance)

        def record_flush():
            flushes_before_parent.append(not parent_scheduled)
            return original_flush()

        monkeypatch.setattr(session, "delete", record_delete)
        monkeypatch.setattr(session, "flush", record_flush)
        outcome = reconcile_deleting_job(session, successful_job, fake_qbit)

    assert outcome == DeletionOutcome.DELETED
    assert flushes_before_parent[0]
    assert all(not path.exists() for path in successful_paths)
    with Session(engine) as session:
        assert session.get(MediaJob, successful_job_id) is None
        assert all(session.get(OrganizedFile, row_id) is None for row_id in successful_ledger_ids)
    assert fake_qbit.deleted_hashes == ["successful"]


async def test_generic_polling_never_converts_deleting_to_failed_on_recovery_discovery_error(
    tmp_path, monkeypatch
):
    engine = make_engine()
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.MOVIE, title="Movie", year=2020,
            release_title="Movie.2020", qbit_hash="hash", category="skald-movie",
            status=JobStatus.DELETING,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    with Session(engine) as session:
        original_exec = session.exec
        exec_calls = 0

        def fail_deleting_discovery(statement, *args, **kwargs):
            nonlocal exec_calls
            exec_calls += 1
            # poll_once's discovery block issues exactly 3 queries in order
            # (active jobs, organizing jobs, deleting jobs); fail the third.
            if exec_calls == 3:
                raise RuntimeError("database is locked")
            return original_exec(statement, *args, **kwargs)

        monkeypatch.setattr(session, "exec", fail_deleting_discovery)
        await poll_once(session, FakeQbit({}), str(tmp_path / "movies"), str(tmp_path / "tv"))

    with Session(engine) as session:
        job = session.get(MediaJob, job_id)
        assert job.status == JobStatus.DELETING
