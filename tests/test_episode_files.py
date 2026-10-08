import pytest
from sqlmodel import Session, SQLModel

from skald.config import Settings
from skald.db import get_engine
from skald.episode_files import AmbiguousEpisodeMarkers, resolve_file_episodes
from skald.indexer.base import ReleaseResult
from skald.models import MediaType
from skald.organizer import build_tv_pack_targets
from skald.qbittorrent import TorrentFile
from skald.services.grab import create_media_job

from tests.test_grab_service import SelectiveRecordingQbit

RU = "01. Химкинские ведьмы.2025.WEB-DL 1080p.Files-x.mkv"


@pytest.mark.parametrize(
    ("path", "default", "expected"),
    [
        (RU, 1, (1, (1,))),
        (f"Химкинские ведьмы.2025.WEB-DL 1080p.Files-x/{RU}", 1, (1, (1,))),
        ("001_name.mkv", 2, (2, (1,))),
        ("01 - Name.mkv", 1, (1, (1,))),
        ("2025.Show.mkv", 1, (None, ())),
        ("1080p.Show.mkv", 1, (None, ())),
        ("Серия 05.mkv", 1, (1, (5,))),
        ("Show - 5 серия.mkv", 1, (1, (5,))),
        ("Show.Episode 06.mkv", 1, (1, (6,))),
        ("Show.Ep07.mkv", 1, (1, (7,))),
        ("Show.E07.mkv", 1, (1, (7,))),
        ("Show.S02E03.mkv", 1, (2, (3,))),
        ("Show.S02E03.mkv", None, (2, (3,))),
        (RU, None, (None, ())),
        ("Sample/01.mkv", 1, (None, ())),
    ],
)
def test_resolve_file_episodes(path, default, expected):
    assert resolve_file_episodes(path, default) == expected


def test_resolve_file_episodes_ambiguous():
    with pytest.raises(AmbiguousEpisodeMarkers):
        resolve_file_episodes("Show.E01.E02.mkv", 1)


def test_organizer_maps_leading_number_files(tmp_path):
    from pathlib import Path

    sources = [Path("/dl/01. X.mkv"), Path("/dl/02. X.mkv")]
    mappings = build_tv_pack_targets(str(tmp_path / "tv"), "Show", sources, 1)
    assert [t.name for _, t in mappings] == ["Show - S01E01.mkv", "Show - S01E02.mkv"]


def _pack_files():
    folder = "Химкинские ведьмы.2025.WEB-DL 1080p.Files-x"
    return [
        TorrentFile(index=i, name=f"{folder}/{i:02d}. Химкинские ведьмы.2025.WEB-DL 1080p.Files-x.mkv")
        for i in range(1, 18)
    ]


def _grab(tmp_path, targets):
    engine = get_engine(str(tmp_path / "grab.db"))
    SQLModel.metadata.create_all(engine)
    qbit = SelectiveRecordingQbit([_pack_files()])
    release = ReleaseResult("Show S1E1-17", "fake", 1, 5, 0, "magnet:?one")
    with Session(engine) as session:
        create_media_job(
            session, qbit, release, media_type=MediaType.TV, title="Show", season=1,
            episode=targets[0], target_episode_numbers=targets, settings=Settings(),
        )
    return qbit


def test_grab_selects_leading_number_pack_files(tmp_path):
    qbit = _grab(tmp_path, tuple(range(1, 18)))
    assert qbit.priority_calls[1] == ("fakehash", list(range(1, 18)), 1)


def test_grab_selects_single_leading_number_file(tmp_path):
    qbit = _grab(tmp_path, (3,))
    assert qbit.priority_calls[1] == ("fakehash", [3], 1)
