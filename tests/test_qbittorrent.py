import hashlib

import httpx
import pytest
import qbittorrentapi

from skald.qbittorrent import QbittorrentClient, TorrentFile, extract_hash_from_magnet


def test_extract_hash_from_magnet():
    magnet = "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD&dn=Test"
    assert extract_hash_from_magnet(magnet) == "aabbccddeeff00112233445566778899aabbccdd"


def test_extract_hash_from_magnet_returns_none_for_non_magnet():
    assert extract_hash_from_magnet("http://example.com/file.torrent") is None


class FakeTorrent:
    def __init__(self, hash_, progress=0.0, state="downloading", content_path="", save_path=""):
        self.hash = hash_
        self.progress = progress
        self.state = state
        self.content_path = content_path
        self.save_path = save_path


class FakeQbitApi:
    def __init__(self):
        self.logged_in = False
        self.added = []
        self.deleted = []
        self.file_priorities = []
        self.resumed = []
        self.files = []
        self.torrents = []

    def auth_log_in(self):
        self.logged_in = True

    def torrents_add(self, urls, category, is_paused=False):
        self.added.append((urls, category, is_paused))
        self.torrents.append(FakeTorrent("newhash123"))

    def torrents_info(self, category=None, torrent_hashes=None):
        if torrent_hashes:
            return [t for t in self.torrents if t.hash == torrent_hashes]
        return list(self.torrents)

    def torrents_delete(self, delete_files=None, torrent_hashes=None):
        self.deleted.append((delete_files, torrent_hashes))

    def torrents_files(self, torrent_hash):
        return self.files

    def torrents_file_priority(self, torrent_hash, file_ids, priority):
        self.file_priorities.append((torrent_hash, file_ids, priority))

    def torrents_resume(self, torrent_hashes):
        self.resumed.append(torrent_hashes)


class ConflictQbitApi(FakeQbitApi):
    """Simulates qBittorrent rejecting a duplicate torrent with 409."""

    def torrents_add(self, urls, category, is_paused=False):
        self.added.append((urls, category, is_paused))
        raise qbittorrentapi.exceptions.Conflict409Error("Conflict")


def test_add_torrent_extracts_hash_from_magnet():
    fake = FakeQbitApi()
    client = QbittorrentClient(
        host="http://localhost:8080", username="admin", password="pw",
        client_factory=lambda: fake,
    )
    magnet = "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD&dn=Test"

    torrent_hash = client.add_torrent(magnet, category="skald-movie")

    assert torrent_hash == "aabbccddeeff00112233445566778899aabbccdd"
    assert fake.logged_in
    assert fake.added == [(magnet, "skald-movie", False)]


def test_add_torrent_magnet_ignores_conflict_for_duplicate():
    fake = ConflictQbitApi()
    client = QbittorrentClient(
        host="http://localhost:8080", username="admin", password="pw",
        client_factory=lambda: fake,
    )
    magnet = "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD&dn=Test"

    torrent_hash = client.add_torrent(magnet, category="skald-movie")

    assert torrent_hash == "aabbccddeeff00112233445566778899aabbccdd"


INFO = b"d6:lengthi5e4:name1:a12:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"e"
TORRENT = b"d8:announce3:foo4:info" + INFO + b"7:comment2:hie"
TORRENT_HASH = hashlib.sha1(INFO).hexdigest()
SECRET_URL = "http://localhost:9117/dl/x/?jackett_apikey=SECRETKEY&file=a"


class UploadQbitApi(FakeQbitApi):
    def __init__(self, add_result="Ok.", appear=True, conflict=False):
        super().__init__()
        self.uploads = []
        self.add_result = add_result
        self.appear = appear
        self.conflict = conflict

    def torrents_add(self, urls=None, category=None, is_paused=False, torrent_files=None):
        if urls is not None:
            self.added.append((urls, category, is_paused))
            return "Ok."
        self.uploads.append((torrent_files, category, is_paused))
        if self.conflict:
            raise qbittorrentapi.exceptions.Conflict409Error("Conflict")
        if self.appear:
            self.torrents.append(FakeTorrent(TORRENT_HASH))
        return self.add_result


def _response(status=200, content=b"", location=None):
    headers = {"location": location} if location else {}
    return httpx.Response(status, content=content, headers=headers)


def _client(fake, response):
    seen = []

    def http_get(url):
        seen.append(url)
        return response

    client = QbittorrentClient(
        host="http://localhost:8080", username="admin", password="pw",
        client_factory=lambda: fake, http_get=http_get, sleep=lambda s: None,
    )
    return client, seen


def test_add_torrent_downloads_file_and_uploads_bytes():
    fake = UploadQbitApi()
    client, seen = _client(fake, _response(content=TORRENT))

    torrent_hash = client.add_torrent_paused(SECRET_URL, category="skald-movie")

    assert torrent_hash == TORRENT_HASH
    assert seen == [SECRET_URL]
    assert fake.uploads == [(TORRENT, "skald-movie", True)]
    assert fake.added == []


def test_add_torrent_redirect_to_magnet_uses_magnet_path():
    fake = UploadQbitApi()
    magnet = "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD&dn=T"
    client, _ = _client(fake, _response(302, location=magnet))

    torrent_hash = client.add_torrent(SECRET_URL, category="c")

    assert torrent_hash == "aabbccddeeff00112233445566778899aabbccdd"
    assert fake.added == [(magnet, "c", False)]
    assert fake.uploads == []


def test_add_torrent_file_conflict_returns_hash():
    fake = UploadQbitApi(conflict=True)
    client, _ = _client(fake, _response(content=TORRENT))

    assert client.add_torrent(SECRET_URL, category="c") == TORRENT_HASH


def test_add_torrent_http_error_does_not_leak_url():
    fake = UploadQbitApi()
    client, _ = _client(fake, _response(500))

    with pytest.raises(RuntimeError) as error:
        client.add_torrent(SECRET_URL, category="c")

    assert "SECRETKEY" not in str(error.value)
    assert "localhost" not in str(error.value)
    assert "500" in str(error.value)


def test_add_torrent_invalid_bencode_raises():
    fake = UploadQbitApi()
    client, _ = _client(fake, _response(content=b"<html>not a torrent</html>"))

    with pytest.raises(RuntimeError, match="not a valid torrent"):
        client.add_torrent(SECRET_URL, category="c")
    assert fake.uploads == []


def test_add_torrent_never_appearing_raises_without_real_sleep():
    fake = UploadQbitApi(appear=False)
    client, _ = _client(fake, _response(content=TORRENT))

    with pytest.raises(RuntimeError, match="did not report a new torrent"):
        client.add_torrent(SECRET_URL, category="c")


def test_add_torrent_fails_result_raises_when_not_present():
    fake = UploadQbitApi(add_result="Fails.", appear=False)
    client, _ = _client(fake, _response(content=TORRENT))

    with pytest.raises(RuntimeError):
        client.add_torrent(SECRET_URL, category="c")


def test_delete_torrent_calls_api_with_delete_files():
    fake = FakeQbitApi()
    client = QbittorrentClient(
        host="http://localhost:8080", username="admin", password="pw",
        client_factory=lambda: fake,
    )

    client.delete_torrent("hash1")

    assert fake.deleted == [(True, "hash1")]
    assert fake.logged_in is True


def test_get_status_maps_fields():
    fake = FakeQbitApi()
    fake.torrents.append(
        FakeTorrent("hash1", progress=1.0, state="uploading",
                    content_path="/downloads/movie", save_path="/downloads")
    )
    client = QbittorrentClient(
        host="http://localhost:8080", username="admin", password="pw",
        client_factory=lambda: fake,
    )

    status = client.get_status("hash1")

    assert status.progress == 1.0
    assert status.is_complete is True
    assert status.content_path == "/downloads/movie"


def test_get_status_not_complete_while_downloading():
    fake = FakeQbitApi()
    fake.torrents.append(
        FakeTorrent("hash1", progress=0.4, state="downloading",
                    content_path="/downloads/movie", save_path="/downloads")
    )
    client = QbittorrentClient(
        host="http://localhost:8080", username="admin", password="pw",
        client_factory=lambda: fake,
    )

    status = client.get_status("hash1")

    assert status.is_complete is False


def test_add_paused_torrent_returns_magnet_hash_and_requests_paused_add():
    fake = FakeQbitApi()
    client = QbittorrentClient(
        host="http://localhost:8080", username="admin", password="pw",
        client_factory=lambda: fake,
    )
    magnet = "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD&dn=Test"

    torrent_hash = client.add_torrent_paused(magnet, category="skald-tv")

    assert torrent_hash == "aabbccddeeff00112233445566778899aabbccdd"
    assert fake.added == [(magnet, "skald-tv", True)]


def test_torrent_files_priorities_and_resume_use_file_indexes():
    fake = FakeQbitApi()
    fake.files = [
        type("ApiFile", (), {"index": 8, "name": "Show.S01E03.mkv"})(),
        type("ApiFile", (), {"index": 21, "name": "Show.S01E04.mkv"})(),
    ]
    client = QbittorrentClient(
        host="http://localhost:8080", username="admin", password="pw",
        client_factory=lambda: fake,
    )

    assert client.get_torrent_files("hash1") == [
        TorrentFile(index=8, name="Show.S01E03.mkv"),
        TorrentFile(index=21, name="Show.S01E04.mkv"),
    ]
    client.set_file_priority("hash1", [8, 21], priority=0)
    client.set_file_priority("hash1", [8], priority=1)
    client.resume_torrent("hash1")

    assert fake.file_priorities == [("hash1", [8, 21], 0), ("hash1", [8], 1)]
    assert fake.resumed == ["hash1"]
