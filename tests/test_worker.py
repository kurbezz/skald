from sqlmodel import Session, SQLModel, create_engine, select

from skald.models import JobStatus, MediaJob, MediaType
from skald.qbittorrent import TorrentStatus
from skald.worker import poll_once


class FakeQbit:
    def __init__(self, statuses: dict[str, TorrentStatus]):
        self.statuses = statuses

    def get_status(self, torrent_hash: str) -> TorrentStatus:
        return self.statuses[torrent_hash]


def make_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
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
