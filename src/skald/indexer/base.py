from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ReleaseResult:
    title: str
    indexer: str
    size_bytes: int
    seeders: int
    leechers: int
    download_url: str
    published_at: str | None = None


class IndexerClient(ABC):
    @abstractmethod
    async def search(self, query: str) -> list["ReleaseResult"]:
        raise NotImplementedError
