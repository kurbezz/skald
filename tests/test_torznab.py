from pathlib import Path

import httpx
import respx

from skald.indexer.torznab import TorznabIndexer, parse_torznab_xml

FIXTURE = Path(__file__).parent / "fixtures" / "torznab_response.xml"


def test_parse_torznab_xml():
    results = parse_torznab_xml(FIXTURE.read_text())

    assert len(results) == 1
    result = results[0]
    assert result.title == "The.Matrix.1999.1080p.BluRay.x264-GROUP"
    assert result.indexer == "SomeIndexer"
    assert result.size_bytes == 1500000000
    assert result.seeders == 120
    assert result.leechers == 10
    assert result.download_url.startswith("magnet:")


@respx.mock
async def test_torznab_indexer_search():
    route = respx.get(
        "http://jackett.local/api/v2.0/indexers/all/results/torznab/api"
    ).mock(return_value=httpx.Response(200, text=FIXTURE.read_text()))

    indexer = TorznabIndexer(base_url="http://jackett.local", api_key="key123")
    results = await indexer.search("the matrix")

    assert route.called
    assert route.calls.last.request.url.params["apikey"] == "key123"
    assert route.calls.last.request.url.params["q"] == "the matrix"
    assert len(results) == 1
