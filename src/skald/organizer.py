import errno
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from skald.lifecycle import FileIdentity, file_identity, identity_matches

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v"}
EPISODE_MARKER = re.compile(r"s(\d{2})[._-]?e(\d{2})(?!\d)", re.IGNORECASE)


class TvPackError(Exception):
    def __init__(
        self,
        detail: str,
        *,
        attempt_owned_paths: tuple[Path, ...] = (),
        explicitly_unowned_paths: tuple[Path, ...] = (),
    ):
        super().__init__(detail)
        self.attempt_owned_paths = attempt_owned_paths
        self.explicitly_unowned_paths = explicitly_unowned_paths


@dataclass(frozen=True)
class LinkFileResult:
    attempt_owned: bool


class LinkFileError(OSError):
    def __init__(
        self,
        write_error: OSError,
        attempt_owned: bool,
        cleanup_error: OSError | None = None,
        explicitly_unowned: bool = False,
    ):
        detail = str(write_error)
        if cleanup_error:
            detail += f"; cleanup failed: {cleanup_error}"
        super().__init__(detail)
        self.attempt_owned = attempt_owned
        self.explicitly_unowned = explicitly_unowned
        self.write_error = write_error
        self.cleanup_error = cleanup_error


@dataclass(frozen=True)
class OwnedPathIdentity:
    path: Path
    identity: FileIdentity


@dataclass(frozen=True)
class FileOperationOutcome:
    attempt_owned_paths: tuple[Path, ...] = ()
    attempt_owned_identities: tuple[OwnedPathIdentity, ...] = ()
    explicitly_unowned_paths: tuple[Path, ...] = ()
    cleanup_errors: tuple[str, ...] = ()


class FileOperationError(OSError):
    def __init__(self, write_error: OSError, outcome: FileOperationOutcome):
        detail = str(write_error)
        if outcome.cleanup_errors:
            detail += f"; cleanup failed: {'; '.join(outcome.cleanup_errors)}"
        super().__init__(detail)
        self.write_error = write_error
        self.outcome = outcome


@dataclass(frozen=True)
class StagedFile:
    staging_path: Path
    identity: FileIdentity
    outcome: FileOperationOutcome


@dataclass(frozen=True)
class PublishedFile:
    target: Path
    identity: FileIdentity
    outcome: FileOperationOutcome


@dataclass(frozen=True)
class CleanupOutcome:
    removed: bool
    foreign: bool
    error: str | None = None


def movie_target_path(movies_root: str, title: str, year: int, source_ext: str) -> Path:
    folder_name = f"{title} ({year})"
    file_name = f"{title} ({year}){source_ext}"
    return Path(movies_root) / folder_name / file_name


def tv_target_path(tv_root: str, series: str, season: int, episode: int, source_ext: str) -> Path:
    season_folder = f"Season {season:02d}"
    file_name = f"{series} - S{season:02d}E{episode:02d}{source_ext}"
    return Path(tv_root) / series / season_folder / file_name


def build_tv_pack_targets(tv_root: str, series: str, video_files: list[Path]) -> list[tuple[Path, Path]]:
    mappings = []
    targets = set()
    for source in video_files:
        markers = list(EPISODE_MARKER.finditer(source.name))
        if len(markers) > 1:
            raise TvPackError(f"Ambiguous episode markers in {source.name}")
        if not markers:
            continue
        season, episode = (int(value) for value in markers[0].groups())
        target = tv_target_path(tv_root, series, season, episode, source.suffix)
        if target in targets:
            raise TvPackError(f"Duplicate target path: {target}")
        if target.exists():
            raise TvPackError(f"Target already exists: {target}")
        targets.add(target)
        mappings.append((source, target))
    if not mappings:
        raise TvPackError("No video files with SxxEyy episode markers were found")
    return mappings


def link_tv_pack(mappings: list[tuple[Path, Path]]) -> list[Path]:
    created = []
    for source, target in mappings:
        try:
            result = link_file(source, target)
            if getattr(result, "attempt_owned", True):
                created.append(target)
        except OSError as exc:
            current_target_owned = getattr(exc, "attempt_owned", False)
            current_target_unowned = getattr(exc, "explicitly_unowned", False)
            if current_target_owned:
                created.append(target)
            cleanup_errors = []
            for created_target in reversed(created):
                try:
                    remove_organized_file(created_target)
                except OSError as cleanup_exc:
                    cleanup_errors.append(str(cleanup_exc))
            detail = f"Failed to organize TV pack: {exc}"
            if cleanup_errors:
                detail += f"; cleanup failed: {'; '.join(cleanup_errors)}"
            raise TvPackError(
                detail,
                attempt_owned_paths=tuple(created),
                explicitly_unowned_paths=(target,) if current_target_unowned else (),
            ) from exc
    return created


def staging_path_for(target: Path, operation_token: str) -> Path:
    """Return a private staging path on the target filesystem."""
    return target.parent / ".skald-staging" / operation_token / f"{target.name}.part"


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def cleanup_owned_file(path: Path, identity: FileIdentity) -> CleanupOutcome:
    """Remove a file only when it still has this attempt's identity."""
    try:
        if not path.exists():
            return CleanupOutcome(removed=True, foreign=False)
        if not identity_matches(path, identity):
            return CleanupOutcome(removed=False, foreign=True)
        path.unlink()
        _fsync_directory(path.parent)
        return CleanupOutcome(removed=True, foreign=False)
    except OSError as exc:
        return CleanupOutcome(removed=False, foreign=False, error=str(exc))


def _fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_failure(
    write_error: OSError,
    staging_path: Path,
    identity: FileIdentity | None,
    *,
    explicitly_unowned: bool = False,
) -> FileOperationError:
    cleanup_errors = ()
    owned_paths = ()
    if identity is not None:
        owned_paths = (staging_path,)
        cleanup = cleanup_owned_file(staging_path, identity)
        if cleanup.error:
            cleanup_errors = (cleanup.error,)
    return FileOperationError(
        write_error,
        FileOperationOutcome(
            attempt_owned_paths=owned_paths,
            attempt_owned_identities=(OwnedPathIdentity(staging_path, identity),)
            if identity is not None else (),
            explicitly_unowned_paths=(staging_path,) if explicitly_unowned else (),
            cleanup_errors=cleanup_errors,
        ),
    )


def stage_file(source: Path, staging_path: Path) -> StagedFile:
    """Create and fsync a private staged file without touching its public target."""
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    if staging_path.exists():
        raise _stage_failure(
            FileExistsError(f"Staging path already exists: {staging_path}"),
            staging_path,
            None,
            explicitly_unowned=True,
        )

    try:
        os.link(source, staging_path)
        identity = file_identity(staging_path)
        try:
            _fsync_path(staging_path)
            _fsync_directory(staging_path.parent)
        except OSError as exc:
            raise _stage_failure(exc, staging_path, identity) from exc
        return StagedFile(
            staging_path=staging_path,
            identity=identity,
            outcome=FileOperationOutcome(
                attempt_owned_paths=(staging_path,),
                attempt_owned_identities=(OwnedPathIdentity(staging_path, identity),),
            ),
        )
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            if isinstance(exc, FileOperationError):
                raise
            raise _stage_failure(
                exc,
                staging_path,
                None,
                explicitly_unowned=exc.errno == errno.EEXIST,
            ) from exc

    identity = None
    try:
        with source.open("rb") as source_file:
            descriptor = os.open(staging_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            try:
                stat_result = os.fstat(descriptor)
            except OSError as fstat_error:
                # O_EXCL guarantees we created staging_path, so it is
                # unconditionally attempt-owned even though we could not
                # capture its identity; close the fd and surface it as
                # owned so it is never silently unreclaimable.
                os.close(descriptor)
                cleanup_error = None
                try:
                    staging_path.unlink()
                except OSError as unlink_error:
                    cleanup_error = unlink_error
                raise FileOperationError(
                    fstat_error,
                    FileOperationOutcome(
                        attempt_owned_paths=(staging_path,),
                        cleanup_errors=(str(cleanup_error),) if cleanup_error else (),
                    ),
                ) from fstat_error
            identity = FileIdentity(device=stat_result.st_dev, inode=stat_result.st_ino)
            with os.fdopen(descriptor, "wb") as staging_file:
                shutil.copyfileobj(source_file, staging_file)
                staging_file.flush()
                os.fsync(staging_file.fileno())
        shutil.copystat(source, staging_path)
        _fsync_path(staging_path)
        _fsync_directory(staging_path.parent)
    except FileOperationError:
        raise
    except OSError as exc:
        raise _stage_failure(
            exc,
            staging_path,
            identity,
            explicitly_unowned=identity is None and exc.errno == errno.EEXIST,
        ) from exc
    return StagedFile(
        staging_path=staging_path,
        identity=identity,
        outcome=FileOperationOutcome(
            attempt_owned_paths=(staging_path,),
            attempt_owned_identities=(OwnedPathIdentity(staging_path, identity),),
        ),
    )


def publish_staged_file(staged: StagedFile, target: Path) -> PublishedFile:
    """Publish a staged file exclusively, without replacing a public target."""
    if not identity_matches(staged.staging_path, staged.identity):
        raise FileOperationError(
            OSError(f"Staging identity mismatch: {staged.staging_path}"),
            FileOperationOutcome(explicitly_unowned_paths=(staged.staging_path,)),
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(staged.staging_path, target)
    except OSError as exc:
        raise FileOperationError(
            exc,
            FileOperationOutcome(
                attempt_owned_paths=(staged.staging_path,),
                attempt_owned_identities=(OwnedPathIdentity(staged.staging_path, staged.identity),),
                explicitly_unowned_paths=(target,) if exc.errno == errno.EEXIST else (),
            ),
        ) from exc

    # A hardlink to staging_path must share its (device, inode); assert this
    # invariant instead of trusting a fresh stat of the target path, which
    # could observe a different file if something replaced it immediately
    # after the link call.
    observed_identity = file_identity(target)
    if observed_identity != staged.identity:
        raise FileOperationError(
            OSError(f"Published identity mismatch: {target}"),
            FileOperationOutcome(
                attempt_owned_paths=(staged.staging_path,),
                attempt_owned_identities=(OwnedPathIdentity(staged.staging_path, staged.identity),),
                explicitly_unowned_paths=(target,),
            ),
        )
    target_identity = staged.identity
    try:
        _fsync_path(target)
        _fsync_directory(target.parent)
    except OSError as exc:
        cleanup = cleanup_owned_file(target, target_identity)
        raise FileOperationError(
            exc,
            FileOperationOutcome(
                attempt_owned_paths=(staged.staging_path, target),
                attempt_owned_identities=(
                    OwnedPathIdentity(staged.staging_path, staged.identity),
                    OwnedPathIdentity(target, target_identity),
                ),
                cleanup_errors=(cleanup.error,) if cleanup.error else (),
            ),
        ) from exc

    staging_cleanup = cleanup_owned_file(staged.staging_path, staged.identity)
    outcome = FileOperationOutcome(
        attempt_owned_paths=(target,)
        if staging_cleanup.removed or staging_cleanup.foreign else (staged.staging_path, target),
        attempt_owned_identities=(OwnedPathIdentity(target, target_identity),)
        if staging_cleanup.removed or staging_cleanup.foreign else (
            OwnedPathIdentity(staged.staging_path, staged.identity),
            OwnedPathIdentity(target, target_identity),
        ),
        explicitly_unowned_paths=(staged.staging_path,) if staging_cleanup.foreign else (),
        cleanup_errors=(staging_cleanup.error,) if staging_cleanup.error else (),
    )
    return PublishedFile(target=target, identity=target_identity, outcome=outcome)


def find_video_files(content_path: Path) -> list[Path]:
    if content_path.is_file():
        return [content_path] if content_path.suffix.lower() in VIDEO_EXTENSIONS else []
    return sorted(
        p for p in content_path.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )


def link_file(source: Path, target: Path) -> LinkFileResult:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        error = FileExistsError(f"Target already exists: {target}")
        error.attempt_owned = False
        error.explicitly_unowned = True
        raise error
    try:
        os.link(source, target)
        return LinkFileResult(attempt_owned=True)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            exc.attempt_owned = False
            exc.explicitly_unowned = exc.errno == errno.EEXIST
            raise

    created_identity = None
    try:
        with source.open("rb") as source_file:
            target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            target_stat = os.fstat(target_fd)
            created_identity = (target_stat.st_dev, target_stat.st_ino)
            with os.fdopen(target_fd, "wb") as target_file:
                shutil.copyfileobj(source_file, target_file)
        shutil.copystat(source, target)
    except OSError as write_error:
        cleanup_error = None
        attempt_owned = False
        if created_identity is not None:
            try:
                current_stat = target.stat()
                if (current_stat.st_dev, current_stat.st_ino) == created_identity:
                    attempt_owned = True
                    target.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                attempt_owned = True
                cleanup_error = exc
        raise LinkFileError(
            write_error,
            attempt_owned,
            cleanup_error,
            explicitly_unowned=created_identity is None and write_error.errno == errno.EEXIST,
        ) from write_error
    return LinkFileResult(attempt_owned=True)


def prune_empty_parent(path: Path) -> None:
    """Remove `path`'s parent directory if it exists and is now empty.

    Single-level only (does not walk further upward); best-effort (any
    `OSError`, e.g. a race with something else populating the directory, is
    swallowed). Shared by scalar (`remove_organized_file`) and pack
    (identity-checked) deletion so both prune library directory trees the
    same way.
    """
    parent = path.parent
    try:
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


def remove_organized_file(path: Path) -> None:
    """Remove an organized library file and its parent folder if it's now empty."""
    if not path.exists():
        return
    path.unlink()
    prune_empty_parent(path)
