from sqlmodel import Session, SQLModel

from skald.config import Settings
from skald.db import get_engine
from skald.indexer.base import ReleaseResult
from skald.models import MediaType
import pytest

from skald.qbittorrent import TorrentFile
from skald.services.grab import TargetTorrentFileNotFoundError, create_media_job


class RecordingQbit:
    def __init__(self):
        self.add_calls = []

    def add_torrent(self, download_url: str, category: str) -> str:
        self.add_calls.append((download_url, category))
        return "fakehash"


class SelectiveRecordingQbit(RecordingQbit):
    def __init__(self, file_responses):
        super().__init__()
        self.file_responses = iter(file_responses)
        self.paused_add_calls = []
        self.priority_calls = []
        self.resumed = []

    def add_torrent_paused(self, download_url: str, category: str) -> str:
        self.paused_add_calls.append((download_url, category))
        return "fakehash"

    def get_torrent_files(self, torrent_hash: str) -> list[TorrentFile]:
        return next(self.file_responses)

    def set_file_priority(self, torrent_hash: str, file_indexes: list[int], priority: int) -> None:
        self.priority_calls.append((torrent_hash, file_indexes, priority))

    def resume_torrent(self, torrent_hash: str) -> None:
        self.resumed.append(torrent_hash)


def test_create_movie_job_adds_torrent_then_persists_job(tmp_path):
    engine = get_engine(str(tmp_path / "grab-service.db"))
    SQLModel.metadata.create_all(engine)
    settings = Settings(category_movie="movies")
    qbit = RecordingQbit()
    release = ReleaseResult("Movie.2026.1080p", "fake", 1, 5, 0, "magnet:?one")

    with Session(engine) as session:
        job = create_media_job(
            session,
            qbit,
            release,
            media_type=MediaType.MOVIE,
            title="Movie",
            year=2026,
            settings=settings,
        )
        assert job.qbit_hash == "fakehash"

    assert qbit.add_calls == [(release.download_url, settings.category_movie)]


def test_create_targeted_tv_job_polls_metadata_disables_other_files_then_resumes(tmp_path):
    engine = get_engine(str(tmp_path / "grab-service.db"))
    SQLModel.metadata.create_all(engine)
    settings = Settings(category_tv="tv")
    qbit = SelectiveRecordingQbit([
        [],
        [
            TorrentFile(index=2, name="Show.S01E01.mkv"),
            TorrentFile(index=9, name="Show.S01E03.mkv"),
            TorrentFile(index=15, name="Show.S01E04.mkv"),
        ],
    ])
    release = ReleaseResult("Show.S01.Pack", "fake", 1, 5, 0, "magnet:?one")
    sleeps = []

    with Session(engine) as session:
        job = create_media_job(
            session, qbit, release, media_type=MediaType.TV, title="Show", season=1,
            episode=3, target_episode_numbers=(3,), settings=settings,
            metadata_poll_attempts=2, sleep=sleeps.append,
        )
        assert job.qbit_hash == "fakehash"

    assert qbit.paused_add_calls == [(release.download_url, "tv")]
    assert sleeps == [1.0]
    assert qbit.priority_calls == [
        ("fakehash", [2, 9, 15], 0),
        ("fakehash", [9], 1),
    ]
    assert qbit.resumed == ["fakehash"]


def test_create_targeted_tv_job_keeps_torrent_paused_when_no_target_file_exists(tmp_path):
    engine = get_engine(str(tmp_path / "grab-service.db"))
    SQLModel.metadata.create_all(engine)
    qbit = SelectiveRecordingQbit([[TorrentFile(index=4, name="Show.S01E02.mkv")]])
    release = ReleaseResult("Show.S01.Pack", "fake", 1, 5, 0, "magnet:?one")

    with Session(engine) as session, pytest.raises(TargetTorrentFileNotFoundError):
        create_media_job(
            session, qbit, release, media_type=MediaType.TV, title="Show", season=1,
            episode=3, target_episode_numbers=(3,), settings=Settings(),
        )

    assert qbit.priority_calls == []
    assert qbit.resumed == []
