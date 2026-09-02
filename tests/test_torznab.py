from pathlib import Path

import httpx
import pytest
import respx

from skald.indexer.torznab import TorznabError, TorznabIndexer, parse_torznab_xml

FIXTURE = Path(__file__).parent / "fixtures" / "torznab_response.xml"

JACKETTINDEXER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <item>
      <title>The.Matrix.1999.1080p.BluRay.x264-GROUP</title>
      <link>magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD</link>
      <size>1500000000</size>
      <jackettindexer id="rutracker-ru">RuTracker.RU</jackettindexer>
      <torznab:attr name="seeders" value="5" />
      <torznab:attr name="peers" value="2" />
    </item>
  </channel>
</rss>
"""

ERROR_XML = '<?xml version="1.0" encoding="UTF-8"?>\n<error code="100" description="Invalid API Key" />'


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


def test_parse_torznab_xml_uses_jackettindexer_element():
    results = parse_torznab_xml(JACKETTINDEXER_XML)

    assert len(results) == 1
    assert results[0].indexer == "RuTracker.RU"


def test_parse_torznab_xml_raises_on_error_response():
    with pytest.raises(TorznabError, match="Invalid API Key"):
        parse_torznab_xml(ERROR_XML)


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
