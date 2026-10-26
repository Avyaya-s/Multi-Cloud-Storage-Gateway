"""Common interface every storage provider adapter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ProviderError(Exception):
    """Any failure talking to a provider (network, auth, server error)."""


class ShardNotFoundError(ProviderError):
    """The requested object does not exist on the provider."""


class StorageProvider(ABC):
    """Stores opaque blobs under opaque keys. Adapters never see file names or plaintext."""

    def __init__(self, name: str, jurisdiction: str) -> None:
        self.name = name
        self.jurisdiction = jurisdiction  # "IN" or "NON_IN"

    async def open(self) -> None:
        """Acquire connections and make sure the bucket/folder exists."""

    async def close(self) -> None:
        """Release connections."""

    @abstractmethod
    async def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """Raise ShardNotFoundError if the key does not exist."""

    @abstractmethod
    async def head(self, key: str) -> bool: ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Deleting a missing key is not an error."""

    @abstractmethod
    async def probe(self) -> float:
        """Round-trip latency in milliseconds; raise ProviderError if unreachable."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, jurisdiction={self.jurisdiction!r})"
