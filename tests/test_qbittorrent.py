import qbittorrentapi

from skald.qbittorrent import QbittorrentClient, extract_hash_from_magnet


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
        self.torrents = []

    def auth_log_in(self):
        self.logged_in = True

    def torrents_add(self, urls, category):
        self.added.append((urls, category))
        self.torrents.append(FakeTorrent("newhash123"))

    def torrents_info(self, category=None, torrent_hashes=None):
        if torrent_hashes:
            return [t for t in self.torrents if t.hash == torrent_hashes]
        return list(self.torrents)

    def torrents_delete(self, delete_files=None, torrent_hashes=None):
        self.deleted.append((delete_files, torrent_hashes))


class ConflictQbitApi(FakeQbitApi):
    """Simulates qBittorrent rejecting a duplicate torrent with 409."""

    def torrents_add(self, urls, category):
        self.added.append((urls, category))
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
    assert fake.added == [(magnet, "skald-movie")]


def test_add_torrent_magnet_ignores_conflict_for_duplicate():
    fake = ConflictQbitApi()
    client = QbittorrentClient(
        host="http://localhost:8080", username="admin", password="pw",
        client_factory=lambda: fake,
    )
    magnet = "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD&dn=Test"

    torrent_hash = client.add_torrent(magnet, category="skald-movie")

    assert torrent_hash == "aabbccddeeff00112233445566778899aabbccdd"


def test_add_torrent_falls_back_to_diff_for_non_magnet():
    fake = FakeQbitApi()
    client = QbittorrentClient(
        host="http://localhost:8080", username="admin", password="pw",
        client_factory=lambda: fake,
    )

    torrent_hash = client.add_torrent("http://example.com/file.torrent", category="skald-movie")

    assert torrent_hash == "newhash123"


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
