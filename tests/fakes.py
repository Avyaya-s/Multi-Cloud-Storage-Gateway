"""In-memory provider for fast unit tests."""

from __future__ import annotations

from gateway.providers.base import ProviderError, ShardNotFoundError, StorageProvider
from gateway.providers.registry import ProviderConfig, ProviderRegistry


class MemoryProvider(StorageProvider):
    def __init__(self, name: str, jurisdiction: str = "IN") -> None:
        super().__init__(name, jurisdiction)
        self.blobs: dict[str, bytes] = {}
        self.down = False
        self.fail_next_puts = 0  # transient failures: the next N puts raise
        self.poison_first_key = False  # every put of the first key seen fails (one dead shard)
        self._poisoned: str | None = None
        self.put_calls = 0
        self.get_calls = 0

    def _check(self) -> None:
        if self.down:
            raise ProviderError(f"{self.name}: down")

    async def put(self, key: str, data: bytes) -> None:
        self.put_calls += 1
        self._check()
        if self.poison_first_key and self._poisoned in (None, key):
            self._poisoned = key
            raise ProviderError(f"{self.name}: cannot store {key}")
        if self.fail_next_puts > 0:
            self.fail_next_puts -= 1
            raise ProviderError(f"{self.name}: transient")
        self.blobs[key] = data

    async def get(self, key: str) -> bytes:
        self.get_calls += 1
        self._check()
        if key not in self.blobs:
            raise ShardNotFoundError(key)
        return self.blobs[key]

    async def head(self, key: str) -> bool:
        self._check()
        return key in self.blobs

    async def delete(self, key: str) -> None:
        self._check()
        self.blobs.pop(key, None)

    async def probe(self) -> float:
        self._check()
        return 0.1


def memory_registry(*names: str) -> ProviderRegistry:
    providers = [MemoryProvider(n, "NON_IN" if "abroad" in n else "IN") for n in names]
    configs = [
        ProviderConfig(name=p.name, type="memory", endpoint="mem://", bucket="b", jurisdiction=p.jurisdiction)
        for p in providers
    ]
    return ProviderRegistry(providers, configs)  # type: ignore[arg-type]
