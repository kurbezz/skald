from pathlib import Path

import pytest

from skald.organizer import find_video_files, link_file, movie_target_path, tv_target_path


def test_movie_target_path():
    path = movie_target_path("/library/movies", "The Matrix", 1999, ".mkv")
    assert path == Path("/library/movies/The Matrix (1999)/The Matrix (1999).mkv")


def test_tv_target_path():
    path = tv_target_path("/library/tv", "Breaking Bad", 1, 5, ".mkv")
    assert path == Path("/library/tv/Breaking Bad/Season 01/Breaking Bad - S01E05.mkv")


def test_find_video_files_in_directory(tmp_path):
    (tmp_path / "movie.mkv").write_text("data")
    (tmp_path / "sample.txt").write_text("data")
    assert find_video_files(tmp_path) == [tmp_path / "movie.mkv"]


def test_find_video_files_single_file(tmp_path):
    video = tmp_path / "movie.mp4"
    video.write_text("data")
    assert find_video_files(video) == [video]


def test_find_video_files_returns_empty_for_non_video_file(tmp_path):
    doc = tmp_path / "readme.txt"
    doc.write_text("data")
    assert find_video_files(doc) == []


def test_link_file_creates_hardlink(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_text("data")
    target = tmp_path / "lib" / "Movie (2020)" / "Movie (2020).mkv"

    link_file(source, target)

    assert target.exists()
    assert target.read_text() == "data"


def test_link_file_raises_if_target_exists(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_text("data")
    target = tmp_path / "target.mkv"
    target.write_text("existing")

    with pytest.raises(FileExistsError):
        link_file(source, target)
