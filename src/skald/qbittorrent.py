import hashlib
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urljoin

import httpx
import qbittorrentapi

MAGNET_HASH_RE = re.compile(r"btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})")
MAX_REDIRECTS = 5
HTTP_TIMEOUT_SECONDS = 30.0


def _default_http_get(url: str) -> httpx.Response:
    return httpx.get(url, follow_redirects=False, timeout=HTTP_TIMEOUT_SECONDS)


def _bencode_end(data: bytes, pos: int, depth: int = 0) -> int:
    """Return the index just past the bencoded value starting at ``pos``."""
    if depth > 64 or pos >= len(data):
        raise ValueError("bad bencode")
    char = data[pos:pos + 1]
    if char == b"i":
        end = data.index(b"e", pos)
        int(data[pos + 1:end])
        return end + 1
    if char in (b"l", b"d"):
        pos += 1
        while data[pos:pos + 1] != b"e":
            pos = _bencode_end(data, pos, depth + 1)
        return pos + 1
    if char.isdigit():
        colon = data.index(b":", pos)
        end = colon + 1 + int(data[pos:colon])
        if end > len(data):
            raise ValueError("bad bencode")
        return end
    raise ValueError("bad bencode")


def torrent_info_hash(data: bytes) -> str:
    """SHA-1 (v1 info-hash) of the raw top-level ``info`` value of a .torrent."""
    try:
        if data[:1] != b"d":
            raise ValueError("not a dict")
        pos = 1
        while data[pos:pos + 1] != b"e":
            key_end = _bencode_end(data, pos)
            colon = data.index(b":", pos)
            key = data[colon + 1:key_end]
            value_end = _bencode_end(data, key_end)
            if key == b"info":
                if data[key_end:key_end + 1] != b"d":
                    raise ValueError("info is not a dict")
                return hashlib.sha1(data[key_end:value_end]).hexdigest()
            pos = value_end
        raise ValueError("no info")
    except (ValueError, IndexError):
        raise RuntimeError("Downloaded file is not a valid torrent") from None

COMPLETE_STATES = {"uploading", "stalledUP", "queuedUP", "forcedUP", "pausedUP"}


def extract_hash_from_magnet(magnet_uri: str) -> Optional[str]:
    match = MAGNET_HASH_RE.search(magnet_uri)
    if not match:
        return None
    return match.group(1).lower()


@dataclass
class TorrentStatus:
    hash: str
    progress: float
    state: str
    content_path: str
    save_path: str

    @property
    def is_complete(self) -> bool:
        return self.progress >= 1.0 or self.state in COMPLETE_STATES


@dataclass(frozen=True)
class TorrentFile:
    """The qBittorrent file metadata needed for selective downloads."""

    index: int
    name: str


class QbittorrentClient:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        client_factory: Optional[Callable[[], object]] = None,
        http_get: Optional[Callable[[str], httpx.Response]] = None,
        sleep: Callable[[float], None] = time.sleep,
        appear_timeout: float = 5.0,
    ):
        self._client = (client_factory or (
            lambda: qbittorrentapi.Client(host=host, username=username, password=password)
        ))()
        self._http_get = http_get or _default_http_get
        self._sleep = sleep
        self._appear_timeout = appear_timeout

    def add_torrent(self, download_url: str, category: str) -> str:
        return self._add_torrent(download_url, category, is_paused=False)

    def add_torrent_paused(self, download_url: str, category: str) -> str:
        """Add a torrent without allowing any files to start downloading."""
        return self._add_torrent(download_url, category, is_paused=True)

    def _add_torrent(self, download_url: str, category: str, *, is_paused: bool) -> str:
        self._client.auth_log_in()
        torrent_bytes: Optional[bytes] = None
        if not download_url.startswith("magnet:"):
            download_url, torrent_bytes = self._fetch(download_url)

        if torrent_bytes is None:
            magnet_hash = extract_hash_from_magnet(download_url)
            if not magnet_hash:
                raise RuntimeError("Unsupported download URL")
            try:
                self._client.torrents_add(
                    urls=download_url, category=category, is_paused=is_paused
                )
            except qbittorrentapi.exceptions.Conflict409Error:
                # Torrent with this hash already exists in qBittorrent (e.g.
                # the user grabbed it before) - not an error, we already
                # know its hash from the magnet URI itself.
                pass
            return magnet_hash

        info_hash = torrent_info_hash(torrent_bytes)
        try:
            result = self._client.torrents_add(
                torrent_files=torrent_bytes, category=category, is_paused=is_paused
            )
        except qbittorrentapi.exceptions.Conflict409Error:
            return info_hash
        if isinstance(result, str) and result.strip().lower().startswith("fail"):
            if not self._client.torrents_info(torrent_hashes=info_hash):
                raise RuntimeError("qBittorrent rejected the torrent file")
            return info_hash

        waited = 0.0
        while True:
            if self._client.torrents_info(torrent_hashes=info_hash):
                return info_hash
            if waited >= self._appear_timeout:
                raise RuntimeError("qBittorrent did not report a new torrent after add")
            self._sleep(0.5)
            waited += 0.5

    def _fetch(self, url: str) -> tuple[str, Optional[bytes]]:
        """Download a .torrent (or resolve a redirect to a magnet) ourselves.

        Returns ``(magnet_uri, None)`` or ``(url, torrent_bytes)``. Error
        messages deliberately never include the URL (it may hold API keys).
        """
        for _ in range(MAX_REDIRECTS + 1):
            try:
                response = self._http_get(url)
            except httpx.HTTPError as exc:
                raise RuntimeError(
                    f"Failed to download torrent file: {type(exc).__name__}"
                ) from None
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                if not location:
                    raise RuntimeError("Torrent download redirect had no location")
                if location.startswith("magnet:"):
                    return location, None
                url = urljoin(url, location)
                if not url.startswith(("http://", "https://")):
                    raise RuntimeError("Torrent download redirected to an unsupported URL")
                continue
            if not 200 <= response.status_code < 300:
                raise RuntimeError(
                    f"Torrent download failed with HTTP {response.status_code}"
                )
            body = response.content
            if body.lstrip().startswith(b"magnet:"):
                return body.strip().decode("utf-8", "replace"), None
            return url, body
        raise RuntimeError("Too many redirects while downloading torrent")

    def get_torrent_files(self, torrent_hash: str) -> list[TorrentFile]:
        self._client.auth_log_in()
        return [
            TorrentFile(index=file.index, name=file.name)
            for file in self._client.torrents_files(torrent_hash=torrent_hash)
        ]

    def set_file_priority(
        self, torrent_hash: str, file_indexes: list[int], priority: int
    ) -> None:
        self._client.auth_log_in()
        self._client.torrents_file_priority(
            torrent_hash=torrent_hash, file_ids=file_indexes, priority=priority
        )

    def resume_torrent(self, torrent_hash: str) -> None:
        self._client.auth_log_in()
        self._client.torrents_resume(torrent_hashes=torrent_hash)

    def delete_torrent(self, torrent_hash: str, delete_files: bool = True) -> None:
        self._client.auth_log_in()
        self._client.torrents_delete(delete_files=delete_files, torrent_hashes=torrent_hash)

    def get_status(self, torrent_hash: str) -> TorrentStatus:
        self._client.auth_log_in()
        torrents = self._client.torrents_info(torrent_hashes=torrent_hash)
        if not torrents:
            raise LookupError(f"Torrent not found: {torrent_hash}")
        t = torrents[0]
        return TorrentStatus(
            hash=t.hash,
            progress=t.progress,
            state=t.state,
            content_path=t.content_path,
            save_path=t.save_path,
        )
