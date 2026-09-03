import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int


def file_identity(path: Path) -> FileIdentity:
    result = path.lstat()
    return FileIdentity(device=result.st_dev, inode=result.st_ino)


def identity_matches(path: Path, identity: FileIdentity) -> bool:
    try:
        return file_identity(path) == identity
    except FileNotFoundError:
        return False


def _lock_for_job(job_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(job_id, threading.Lock())


@contextmanager
def try_job_lock(job_id: int) -> Iterator[bool]:
    """Acquire a process-local job lock without blocking.

    This lock only prevents local worker interleaving; persisted operation
    tokens remain the correctness fence across crashes and processes.
    """
    lock = _lock_for_job(job_id)
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()
