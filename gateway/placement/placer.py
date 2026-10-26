"""Shard placement and upload: at most m shards of a segment per provider, with re-routing.

Outcome rules (CLAUDE.md):
- every segment 6/6            -> ACTIVE
- every segment >= k+1 stored  -> DEGRADED, repair queued
- any segment < k+1 stored     -> ROLLBACK (delete what was uploaded, return an error)
"""

from __future__ import annotations

import asyncio
import enum
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from gateway.core import erasure
from gateway.core.pipeline import EncodedSegment, EncodedShard
from gateway.providers.base import StorageProvider

log = logging.getLogger(__name__)

# Called with (shard_index, new_provider) BEFORE the re-routed put, so metadata
# always names the provider an object may exist on (write-ahead).
RerouteHook = Callable[[int, str], Awaitable[None]]


class Outcome(enum.Enum):
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    ROLLBACK = "ROLLBACK"


@dataclass(frozen=True)
class Reroute:
    shard_index: int
    from_provider: str | None  # None: no provider had room in the initial plan
    to_provider: str


@dataclass(frozen=True)
class SegmentPlacement:
    seg_index: int
    stored: Mapping[int, str]  # shard_index -> provider holding it
    missing: frozenset[int]
    failed_providers: frozenset[str]
    reroutes: tuple[Reroute, ...]


def plan(n_shards: int, providers: Sequence[str], cap: int = erasure.M, offset: int = 0) -> list[str | None]:
    """Round-robin shards over providers (rotated by `offset`), never exceeding `cap` each.

    Shards that cannot be placed under the cap get None.
    """
    if not providers:
        return [None] * n_shards
    start = offset % len(providers)
    order = list(providers[start:]) + list(providers[:start])
    counts: Counter[str] = Counter()
    assignment: list[str | None] = []
    for i in range(n_shards):
        target = order[i % len(order)]
        if counts[target] < cap:
            counts[target] += 1
            assignment.append(target)
        else:
            assignment.append(None)
    return assignment


def segment_ok(stored: int, k: int = erasure.K) -> bool:
    return stored >= k + 1


def classify(stored_per_segment: Iterable[int], k: int = erasure.K, m: int = erasure.M) -> Outcome:
    counts = list(stored_per_segment)
    if not counts or any(not segment_ok(c, k) for c in counts):
        return Outcome.ROLLBACK
    return Outcome.ACTIVE if all(c == k + m for c in counts) else Outcome.DEGRADED


class Placer:
    def __init__(
        self,
        providers: Mapping[str, StorageProvider],
        cap: int = erasure.M,
        attempts: int = 3,
        backoff_s: float = 0.2,
        concurrency: int = 16,
    ) -> None:
        self._providers = providers
        self.cap = cap
        self.attempts = attempts
        self.backoff_s = backoff_s
        self._sem = asyncio.Semaphore(concurrency)

    async def _put(self, provider: str, shard: EncodedShard) -> bool:
        """Put with retries and exponential backoff; True on success."""
        for attempt in range(self.attempts):
            try:
                async with self._sem:
                    await self._providers[provider].put(shard.object_key, shard.data)
                return True
            except Exception as err:  # any provider failure; cancellation still propagates
                log.warning(
                    "put failed provider=%s shard=%d attempt=%d/%d error=%s",
                    provider, shard.index, attempt + 1, self.attempts, type(err).__name__,
                )
                if attempt + 1 < self.attempts:
                    await asyncio.sleep(self.backoff_s * 2**attempt)
        return False

    async def execute(
        self,
        seg: EncodedSegment,
        assignment: Sequence[str | None],
        providers: Sequence[str],
        on_reroute: RerouteHook | None = None,
    ) -> SegmentPlacement:
        """Upload a segment's shards per `assignment`, re-routing failures within the cap."""
        planned = [(sh, p) for sh, p in zip(seg.shards, assignment) if p is not None]
        results = await asyncio.gather(*(self._put(p, sh) for sh, p in planned))

        stored: dict[int, str] = {}
        failed: set[str] = set()
        pending: list[tuple[EncodedShard, str | None]] = [
            (sh, None) for sh, p in zip(seg.shards, assignment) if p is None
        ]
        for (shard, provider), ok in zip(planned, results):
            if ok:
                stored[shard.index] = provider
            else:
                failed.add(provider)
                pending.append((shard, provider))

        reroutes: list[Reroute] = []
        for shard, original in sorted(pending, key=lambda t: t[0].index):
            target = await self._reroute(shard, providers, stored, failed, on_reroute)
            if target is not None:
                stored[shard.index] = target
                reroutes.append(Reroute(shard.index, original, target))
                log.info("re-routed seg=%d shard=%d %s -> %s", seg.seg_index, shard.index, original, target)

        missing = frozenset(sh.index for sh in seg.shards) - frozenset(stored)
        return SegmentPlacement(seg.seg_index, stored, missing, frozenset(failed), tuple(reroutes))

    async def _reroute(
        self,
        shard: EncodedShard,
        providers: Sequence[str],
        stored: Mapping[int, str],
        failed: set[str],
        on_reroute: RerouteHook | None,
    ) -> str | None:
        """Try every provider still under the cap; return the one that took the shard."""
        while True:
            counts = Counter(stored.values())
            candidates = [p for p in providers if p not in failed and counts[p] < self.cap]
            if not candidates:
                return None
            target = min(candidates, key=lambda p: counts[p])  # least-loaded first
            if on_reroute is not None:
                await on_reroute(shard.index, target)
            if await self._put(target, shard):
                return target
            failed.add(target)

    async def delete(self, locations: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
        """Best-effort delete of (provider, object_key) pairs; returns the ones that failed."""
        locations = list(locations)

        async def one(provider: str, key: str) -> bool:
            try:
                async with self._sem:
                    await self._providers[provider].delete(key)
                return True
            except Exception as err:
                log.warning("delete failed provider=%s error=%s", provider, type(err).__name__)
                return False

        results = await asyncio.gather(*(one(p, k) for p, k in locations))
        return {loc for loc, ok in zip(locations, results) if not ok}
