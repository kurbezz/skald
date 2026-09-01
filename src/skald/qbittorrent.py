import re
from dataclasses import dataclass
from typing import Callable, Optional

import qbittorrentapi

MAGNET_HASH_RE = re.compile(r"btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})")

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


class QbittorrentClient:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        client_factory: Optional[Callable[[], object]] = None,
    ):
        self._client = (client_factory or (
            lambda: qbittorrentapi.Client(host=host, username=username, password=password)
        ))()

    def add_torrent(self, download_url: str, category: str) -> str:
        self._client.auth_log_in()
        magnet_hash = extract_hash_from_magnet(download_url)
        if magnet_hash:
            self._client.torrents_add(urls=download_url, category=category)
            return magnet_hash

        before = {t.hash for t in self._client.torrents_info(category=category)}
        self._client.torrents_add(urls=download_url, category=category)
        after = self._client.torrents_info(category=category)
        new_hashes = [t.hash for t in after if t.hash not in before]
        if not new_hashes:
            raise RuntimeError("qBittorrent did not report a new torrent after add")
        return new_hashes[0]

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
