import errno
from pathlib import Path

import pytest

from skald.organizer import (
    CleanupOutcome,
    TvPackError,
    build_tv_pack_targets,
    cleanup_owned_file,
    file_identity,
    find_video_files,
    link_file,
    link_tv_pack,
    movie_target_path,
    remove_organized_file,
    publish_staged_file,
    stage_file,
    staging_path_for,
    tv_target_path,
)


def test_movie_target_path():
    path = movie_target_path("/library/movies", "The Matrix", 1999, ".mkv")
    assert path == Path("/library/movies/The Matrix (1999)/The Matrix (1999).mkv")


def test_tv_target_path():
    path = tv_target_path("/library/tv", "Breaking Bad", 1, 5, ".mkv")
    assert path == Path("/library/tv/Breaking Bad/Season 01/Breaking Bad - S01E05.mkv")


@pytest.mark.parametrize("name, expected", [
    ("Show.S01E02.mkv", (1, 2)),
    ("Show.S01.E02.mkv", (1, 2)),
    ("Show_S01_E02.mkv", (1, 2)),
    ("Show-S01-E02.mkv", (1, 2)),
    ("Show.s01e02.mkv", (1, 2)),
])
def test_build_tv_pack_targets_accepts_supported_episode_markers(tmp_path, name, expected):
    source = tmp_path / name
    source.write_text("data")
    season, episode = expected

    assert build_tv_pack_targets(str(tmp_path / "tv"), "Show", [source]) == [
        (source, tv_target_path(str(tmp_path / "tv"), "Show", season, episode, ".mkv"))
    ]


def test_build_tv_pack_targets_rejects_ambiguous_and_missing_markers(tmp_path):
    ambiguous = tmp_path / "Show.S01E01.S01E02.mkv"
    unmarked = tmp_path / "Show.episode.mkv"
    ambiguous.write_text("data")
    unmarked.write_text("data")

    with pytest.raises(TvPackError, match="Ambiguous episode markers"):
        build_tv_pack_targets(str(tmp_path / "tv"), "Show", [ambiguous])
    with pytest.raises(TvPackError, match="No video files with SxxEyy"):
        build_tv_pack_targets(str(tmp_path / "tv"), "Show", [unmarked])


def test_build_tv_pack_targets_rejects_duplicate_derived_targets(tmp_path):
    first = tmp_path / "Show.S01E02.mkv"
    second = tmp_path / "Show.S01.E02.mkv"
    first.write_text("data")
    second.write_text("data")

    with pytest.raises(TvPackError, match="Duplicate target path"):
        build_tv_pack_targets(str(tmp_path / "tv"), "Show", [first, second])


def test_build_tv_pack_targets_rejects_preexisting_targets(tmp_path):
    source = tmp_path / "Show.S01E02.mkv"
    target = tv_target_path(str(tmp_path / "tv"), "Show", 1, 2, ".mkv")
    source.write_text("data")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("existing")

    with pytest.raises(TvPackError, match="Target already exists"):
        build_tv_pack_targets(str(tmp_path / "tv"), "Show", [source])


def test_build_tv_pack_targets_rejects_three_digit_episode_marker(tmp_path):
    source = tmp_path / "Show.S01E010.mkv"
    source.write_text("data")

    with pytest.raises(TvPackError, match="No video files with SxxEyy"):
        build_tv_pack_targets(str(tmp_path / "tv"), "Show", [source])


def test_link_tv_pack_rolls_back_created_targets_on_link_failure(tmp_path, monkeypatch):
    sources = [tmp_path / f"Show.S01E{episode:02d}.mkv" for episode in range(1, 3)]
    targets = [tmp_path / "tv" / source.name for source in sources]
    for source in sources:
        source.write_text("data")
    calls = 0

    def fake_link_file(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text())

    monkeypatch.setattr("skald.organizer.link_file", fake_link_file)

    with pytest.raises(TvPackError, match="disk full"):
        link_tv_pack(list(zip(sources, targets)))

    assert not targets[0].exists()


def test_link_tv_pack_cleans_the_current_partially_created_target(tmp_path, monkeypatch):
    sources = [tmp_path / f"Show.S01E{episode:02d}.mkv" for episode in range(1, 3)]
    targets = [tmp_path / "tv" / source.name for source in sources]
    for source in sources:
        source.write_text("data")
    original_remove = remove_organized_file

    def fail_after_creating_target(source, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text())
        if target == targets[1]:
            error = OSError("disk full")
            error.attempt_owned = True
            raise error

    def fail_current_target_cleanup(target):
        if target == targets[1]:
            raise OSError("cleanup denied")
        original_remove(target)

    monkeypatch.setattr("skald.organizer.link_file", fail_after_creating_target)
    monkeypatch.setattr("skald.organizer.remove_organized_file", fail_current_target_cleanup)

    with pytest.raises(TvPackError, match="disk full.*cleanup denied"):
        link_tv_pack(list(zip(sources, targets)))

    assert not targets[0].exists()
    assert targets[1].exists()


def test_link_tv_pack_does_not_clean_a_target_owned_by_a_racing_writer(tmp_path, monkeypatch):
    source = tmp_path / "Show.S01E01.mkv"
    target = tmp_path / "tv" / "Show.S01E01.mkv"
    source.write_text("data")

    def race_to_create_target(source, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("racing writer")
        error = FileExistsError(f"Target already exists: {target}")
        error.attempt_owned = False
        error.explicitly_unowned = True
        raise error

    monkeypatch.setattr("skald.organizer.link_file", race_to_create_target)

    with pytest.raises(TvPackError, match="Target already exists") as raised:
        link_tv_pack([(source, target)])

    assert target.read_text() == "racing writer"
    assert raised.value.attempt_owned_paths == ()
    assert raised.value.explicitly_unowned_paths == (target,)


def test_link_tv_pack_registers_a_cleaned_partial_target_as_attempt_owned(tmp_path, monkeypatch):
    sources = [tmp_path / f"Show.S01E{episode:02d}.mkv" for episode in range(1, 3)]
    targets = [tmp_path / "tv" / source.name for source in sources]
    for source in sources:
        source.write_text("data")
    cleanup_calls = []

    def fail_after_link_file_cleans_its_partial_target(source, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text())
        if target == targets[1]:
            target.unlink()
            error = OSError("disk full")
            error.attempt_owned = True
            raise error

    def record_cleanup(target):
        cleanup_calls.append(target)
        remove_organized_file(target)

    monkeypatch.setattr("skald.organizer.link_file", fail_after_link_file_cleans_its_partial_target)
    monkeypatch.setattr("skald.organizer.remove_organized_file", record_cleanup)

    with pytest.raises(TvPackError, match="disk full"):
        link_tv_pack(list(zip(sources, targets)))

    assert cleanup_calls == [targets[1], targets[0]]


def test_staging_keeps_partial_output_private_and_captures_identity(tmp_path):
    source = tmp_path / "downloads" / "Show.S01E01.mkv"
    target = tmp_path / "library" / "Show" / "Season 01" / "Show - S01E01.mkv"
    source.parent.mkdir(parents=True)
    source.write_text("episode")
    staging_path = staging_path_for(target, "operation-token")

    staged = stage_file(source, staging_path)

    assert staged.staging_path == staging_path
    assert staged.identity == file_identity(staging_path)
    assert staged.outcome.attempt_owned_paths == (staging_path,)
    assert not target.exists()


def test_staging_uses_copy_only_after_exdev_link_failure(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    target = tmp_path / "library" / "Show.mkv"
    source.write_text("episode")
    staging_path = staging_path_for(target, "operation-token")
    copy_calls = 0

    def force_exdev(source, target):
        raise OSError(errno.EXDEV, "cross-device link")

    def count_copy(source_file, target_file):
        nonlocal copy_calls
        copy_calls += 1
        target_file.write(source_file.read())

    monkeypatch.setattr("skald.organizer.os.link", force_exdev)
    monkeypatch.setattr("skald.organizer.shutil.copyfileobj", count_copy)

    staged = stage_file(source, staging_path)

    assert copy_calls == 1
    assert staging_path.read_text() == "episode"
    assert not target.exists()


def test_staging_partial_copy_is_cleaned_without_public_output(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    target = tmp_path / "library" / "Show.mkv"
    source.write_text("episode")
    staging_path = staging_path_for(target, "operation-token")

    def force_exdev(source, target):
        raise OSError(errno.EXDEV, "cross-device link")

    def write_then_fail(source_file, target_file):
        target_file.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr("skald.organizer.os.link", force_exdev)
    monkeypatch.setattr("skald.organizer.shutil.copyfileobj", write_then_fail)

    with pytest.raises(OSError, match="disk full") as raised:
        stage_file(source, staging_path)

    assert raised.value.outcome.attempt_owned_paths == (staging_path,)
    owned = raised.value.outcome.attempt_owned_identities
    assert owned[0].path == staging_path
    assert owned[0].identity.device > 0
    assert owned[0].identity.inode > 0
    assert not staging_path.exists()
    assert not target.exists()


def test_publication_is_exclusive_and_preserves_foreign_race_target(tmp_path):
    source = tmp_path / "source.mkv"
    target = tmp_path / "library" / "Show.mkv"
    source.write_text("episode")
    staging_path = staging_path_for(target, "operation-token")
    staged = stage_file(source, staging_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("foreign")

    with pytest.raises(OSError, match="File exists") as raised:
        publish_staged_file(staged, target)

    assert raised.value.outcome.explicitly_unowned_paths == (target,)
    assert target.read_text() == "foreign"
    assert staging_path.exists()


def test_publication_records_identity_and_removes_private_staging_file(tmp_path):
    source = tmp_path / "source.mkv"
    target = tmp_path / "library" / "Show.mkv"
    source.write_text("episode")
    staging_path = staging_path_for(target, "operation-token")
    staged = stage_file(source, staging_path)

    published = publish_staged_file(staged, target)

    assert published.target == target
    assert published.identity == file_identity(target)
    assert published.identity == staged.identity
    assert published.outcome.attempt_owned_paths == (target,)
    assert not staging_path.exists()


def test_staging_and_publication_fsync_parent_directories(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    target = tmp_path / "library" / "Show.mkv"
    source.write_text("episode")
    staging_path = staging_path_for(target, "operation-token")
    fsynced_directories = []

    def record_directory_fsync(directory):
        fsynced_directories.append(directory)

    monkeypatch.setattr("skald.organizer._fsync_directory", record_directory_fsync)

    staged = stage_file(source, staging_path)
    publish_staged_file(staged, target)

    assert fsynced_directories == [staging_path.parent, target.parent, staging_path.parent]


def test_publication_reports_foreign_staging_cleanup_outcome(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    target = tmp_path / "library" / "Show.mkv"
    source.write_text("episode")
    staging_path = staging_path_for(target, "operation-token")
    staged = stage_file(source, staging_path)
    original_cleanup = cleanup_owned_file

    def report_foreign_staging(path, identity):
        if path == staging_path:
            return CleanupOutcome(removed=False, foreign=True)
        return original_cleanup(path, identity)

    monkeypatch.setattr("skald.organizer.cleanup_owned_file", report_foreign_staging)

    published = publish_staged_file(staged, target)

    assert published.outcome.explicitly_unowned_paths == (staging_path,)
    assert published.outcome.attempt_owned_identities[0].identity == published.identity
    assert staging_path.exists()


def test_publication_asserts_published_identity_matches_staged_identity(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    target = tmp_path / "library" / "Show.mkv"
    source.write_text("episode")
    staging_path = staging_path_for(target, "operation-token")
    staged = stage_file(source, staging_path)

    from skald.organizer import FileIdentity

    def mismatched_identity(path):
        if path == target:
            return FileIdentity(device=999999, inode=999999)
        return file_identity(path)

    monkeypatch.setattr("skald.organizer.file_identity", mismatched_identity)

    with pytest.raises(OSError, match="Published identity mismatch"):
        publish_staged_file(staged, target)

    assert target.exists()
    assert staging_path.exists()


def test_staging_copy_fallback_captures_identity_via_fstat_not_path_restat(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    target = tmp_path / "library" / "Show.mkv"
    source.write_text("episode")
    staging_path = staging_path_for(target, "operation-token")

    def force_exdev(source, target):
        raise OSError(errno.EXDEV, "cross-device link")

    restat_calls = []
    original_file_identity = file_identity

    def record_restat(path):
        restat_calls.append(path)
        return original_file_identity(path)

    monkeypatch.setattr("skald.organizer.os.link", force_exdev)
    monkeypatch.setattr("skald.organizer.file_identity", record_restat)

    staged = stage_file(source, staging_path)

    assert staged.identity == file_identity(staging_path)
    # file_identity (path re-stat) must not be used to capture the staging
    # identity itself; it is only acceptable for verification afterwards.
    assert staging_path not in restat_calls


def test_staging_copy_fallback_closes_fd_and_reports_owned_file_when_fstat_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    target = tmp_path / "library" / "Show.mkv"
    source.write_text("episode")
    staging_path = staging_path_for(target, "operation-token")

    def force_exdev(source, target):
        raise OSError(errno.EXDEV, "cross-device link")

    closed_fds = []
    original_close = __import__("os").close

    def fail_fstat(fd):
        raise OSError("fstat failed")

    def record_close(fd):
        closed_fds.append(fd)
        original_close(fd)

    monkeypatch.setattr("skald.organizer.os.link", force_exdev)
    monkeypatch.setattr("skald.organizer.os.fstat", fail_fstat)
    monkeypatch.setattr("skald.organizer.os.close", record_close)

    with pytest.raises(OSError, match="fstat failed") as raised:
        stage_file(source, staging_path)

    assert closed_fds, "descriptor was not closed after fstat failure"
    assert raised.value.outcome.attempt_owned_paths == (staging_path,)
    assert not staging_path.exists()


def test_identity_checked_cleanup_preserves_replaced_file(tmp_path):
    path = tmp_path / "library" / "Show.mkv"
    path.parent.mkdir()
    path.write_text("owned")
    identity = file_identity(path)
    path.unlink()
    path.write_text("foreign replacement")

    cleanup = cleanup_owned_file(path, identity)

    assert cleanup.foreign
    assert not cleanup.removed
    assert path.read_text() == "foreign replacement"


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


def test_link_file_removes_partial_copy_after_cross_device_link_failure(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    target = tmp_path / "target.mkv"
    source.write_text("data")

    def fail_link(source, target):
        raise OSError(errno.EXDEV, "cross-device link")

    def fail_copy(source_file, target_file):
        target_file.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr("skald.organizer.os.link", fail_link)
    monkeypatch.setattr("skald.organizer.shutil.copyfileobj", fail_copy)

    with pytest.raises(OSError, match="disk full"):
        link_file(source, target)

    assert not target.exists()


def test_remove_organized_file_removes_empty_parent(tmp_path):
    organized_file = tmp_path / "Movie (2020)" / "Movie (2020).mkv"
    organized_file.parent.mkdir()
    organized_file.write_text("data")

    remove_organized_file(organized_file)

    assert not organized_file.exists()
    assert not organized_file.parent.exists()


def test_remove_organized_file_keeps_nonempty_parent(tmp_path):
    organized_file = tmp_path / "Movie (2020)" / "Movie (2020).mkv"
    organized_file.parent.mkdir()
    organized_file.write_text("data")
    other_file = organized_file.parent / "extras.mkv"
    other_file.write_text("data")

    remove_organized_file(organized_file)

    assert not organized_file.exists()
    assert organized_file.parent.exists()
    assert other_file.exists()


def test_remove_organized_file_ignores_missing_path(tmp_path):
    remove_organized_file(tmp_path / "missing" / "Movie (2020).mkv")
