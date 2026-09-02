from xml.etree import ElementTree

import httpx

from skald.indexer.base import IndexerClient, ReleaseResult

TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"


class TorznabError(RuntimeError):
    """Raised when the indexer proxy returns a Torznab <error> response
    (e.g. invalid API key) instead of search results."""


def parse_torznab_xml(xml_text: str) -> list[ReleaseResult]:
    root = ElementTree.fromstring(xml_text)

    if root.tag == "error":
        code = root.get("code", "unknown")
        description = root.get("description", "no description")
        raise TorznabError(f"Indexer returned error {code}: {description}")

    results = []
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        size_text = item.findtext("size")
        size_bytes = int(size_text) if size_text and size_text.isdigit() else 0
        pub_date = item.findtext("pubDate")

        seeders = 0
        leechers = 0
        # Jackett reports the source tracker via <jackettindexer>; Prowlarr
        # (and Torznab-spec-strict servers) use a torznab:attr instead.
        indexer_name = item.findtext("jackettindexer") or "unknown"
        for attr in item.findall(f"{TORZNAB_NS}attr"):
            name = attr.get("name")
            value = attr.get("value")
            if name == "seeders" and value is not None:
                seeders = int(value)
            elif name == "peers" and value is not None:
                leechers = int(value)
            elif name == "indexer" and value is not None:
                indexer_name = value

        results.append(
            ReleaseResult(
                title=title,
                indexer=indexer_name,
                size_bytes=size_bytes,
                seeders=seeders,
                leechers=leechers,
                download_url=link,
                published_at=pub_date,
            )
        )
    return results


class TorznabIndexer(IndexerClient):
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def search(self, query: str) -> list[ReleaseResult]:
        url = f"{self.base_url}/api/v2.0/indexers/all/results/torznab/api"
        params = {"apikey": self.api_key, "t": "search", "q": query}
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=30.0)
            response.raise_for_status()
        return parse_torznab_xml(response.text)
