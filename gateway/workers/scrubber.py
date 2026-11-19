"""Scrubber: find missing or corrupt shards and rebuild them from the surviving ones.

Repair needs NO key. Shards are pieces of ciphertext, so a lost shard is rebuilt with
Reed-Solomon alone: decode any k verified shards back to the segment's ciphertext,
re-encode it (zfec is deterministic), and keep only the shards that were lost. Every
rebuilt shard must match the SHA-256 recorded at upload before it is stored.

Two depths:
- quick: HEAD every stored shard (does the object still exist?)
- deep:  download every stored shard and check its SHA-256 (silent corruption)

A provider that does not answer is NOT treated as data loss: its shards are left alone
("unreachable"); moving data off an unhealthy provider is the rebalancer's job.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from gateway.core import crypto, erasure
from gateway.db.models import File, FileStatus, Segment, Shard, ShardStatus
from gateway.db.repo import Repo
from gateway.placement.placer import Placer
from gateway.providers.base import ShardNotFoundError
from gateway.providers.registry import ProviderRegistry

log = logging.getLogger(__name__)

LIVE = (FileStatus.ACTIVE, FileStatus.DEGRADED)
NEEDS_REPAIR = (ShardStatus.MISSING, ShardStatus.CORRUPT, ShardStatus.PENDING)


class ScrubBusyError(Exception):
    """A scrub is already running."""


class Check(enum.Enum):
    OK = "OK"
    MISSING = "MISSING"
    CORRUPT = "CORRUPT"
    UNREACHABLE = "UNREACHABLE"


@dataclass
class ScrubReport:
    deep: bool
    files: int = 0
    shards_checked: int = 0
    missing_found: int = 0
    corrupt_found: int = 0
    unreachable: int = 0  # shards on providers that did not answer; left untouched
    repaired: int = 0
    unrepaired: int = 0
    healed_files: list[str] = field(default_factory=list)
    duration_s: float = 0.0


class Scrubber:
    def __init__(
        self,
        repo: Repo,
        registry: ProviderRegistry,
        placer: Placer,
        concurrency: int = 8,
    ) -> None:
        self.repo = repo
        self.registry = registry
        self.placer = placer
        self._sem = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self, deep: bool = False) -> ScrubReport:
        """Check and repair every live file once."""
        if self._lock.locked():
            raise ScrubBusyError("a scrub is already running")
        async with self._lock:
            report = ScrubReport(deep=deep)
            start = time.perf_counter()
            down: set[str] = set()  # providers that failed during this run: skip them
            for file in await self.repo.live_files():
                await self._scrub_file(file, deep, down, report)
            report.duration_s = round(time.perf_counter() - start, 3)
            await self.repo.add_event("SCRUB", asdict(report))
            log.info("scrub done %s", report)
            return report

    # --- per file / segment -------------------------------------------------------

    async def _scrub_file(self, file: File, deep: bool, down: set[str], report: ScrubReport) -> None:
        report.files += 1
        complete = True
        for seg in await self.repo.get_placement(file.id):
            complete &= await self._scrub_segment(file.id, seg, deep, down, report)
        current = await self.repo.get_file(file.id)
        if current is None or current.status not in LIVE:
            return  # deleted while we worked
        if complete and current.status is FileStatus.DEGRADED:
            await self.repo.set_file_status(file.id, FileStatus.ACTIVE)
            await self.repo.add_event("FILE_HEALED", {"file_id": str(file.id)})
            report.healed_files.append(str(file.id))
        elif not complete and current.status is FileStatus.ACTIVE:
            await self.repo.set_file_status(file.id, FileStatus.DEGRADED)
            await self.repo.add_event("REPAIR_QUEUED", {"file_id": str(file.id), "by": "scrubber"})

    async def _scrub_segment(
        self, file_id: uuid.UUID, seg: Segment, deep: bool, down: set[str], report: ScrubReport
    ) -> bool:
        """Check, then repair, one segment. True if every shard is STORED afterwards."""
        rows = {sh.shard_index: sh for sh in seg.shards}
        bad = {i for i, sh in rows.items() if sh.status in NEEDS_REPAIR or sh.provider_id is None}
        good: dict[int, bytes] = {}

        found = await self._check_shards([rows[i] for i in sorted(rows) if i not in bad], deep, down, good, report)
        bad |= set(found)
        await self.repo.set_shard_statuses({rows[i].id: st for i, st in found.items()})
        if not bad:
            return True

        await self._fetch_good(rows, bad, good, down, seg.k, report)
        if len(good) < seg.k:
            report.unrepaired += len(bad)
            await self.repo.add_event(
                "REPAIR_FAILED",
                {"file_id": str(file_id), "seg_index": seg.seg_index, "readable": len(good), "need": seg.k},
            )
            return False

        rebuilt = await asyncio.to_thread(_rebuild, good, seg)
        current = await self.repo.get_file(file_id)
        if current is None or current.status not in LIVE:
            return True  # deleted meanwhile: do not write new objects for it
        to_repair = sorted(bad)  # _repair_shard removes each fixed index from `bad`
        for index in to_repair:
            await self._repair_shard(file_id, seg, rows, bad, index, rebuilt[index], down, report)
        return not bad

    # --- checking -------------------------------------------------------------------

    async def _check_shards(
        self, shards: Sequence[Shard], deep: bool, down: set[str], good: dict[int, bytes], report: ScrubReport
    ) -> dict[int, ShardStatus]:
        """Check stored shards; return {shard_index: new bad status}. Fills `good` in deep mode."""
        results = await asyncio.gather(*(self._check_one(sh, deep, down) for sh in shards))
        found: dict[int, ShardStatus] = {}
        for sh, (result, data) in zip(shards, results):
            report.shards_checked += 1
            if result is Check.UNREACHABLE:
                report.unreachable += 1
            elif result is Check.MISSING:
                report.missing_found += 1
                found[sh.shard_index] = ShardStatus.MISSING
            elif result is Check.CORRUPT:
                report.corrupt_found += 1
                found[sh.shard_index] = ShardStatus.CORRUPT
            elif data is not None:
                good[sh.shard_index] = data
        return found

    async def _check_one(self, sh: Shard, deep: bool, down: set[str]) -> tuple[Check, bytes | None]:
        provider = sh.provider_id
        assert provider is not None
        if provider in down:
            return Check.UNREACHABLE, None
        try:
            async with self._sem:
                if deep:
                    data = await self.registry[provider].get(sh.object_key)
                else:
                    exists = await self.registry[provider].head(sh.object_key)
                    return (Check.OK if exists else Check.MISSING), None
        except ShardNotFoundError:
            return Check.MISSING, None
        except Exception as err:  # provider unreachable / erroring: not evidence of loss
            log.warning("scrub: provider %s unreachable (%s)", provider, type(err).__name__)
            down.add(provider)
            return Check.UNREACHABLE, None
        if erasure.fingerprint(data) != sh.sha256:
            log.warning("scrub: CORRUPT shard key=%s provider=%s", sh.object_key, provider)
            return Check.CORRUPT, None
        return Check.OK, data

    async def _fetch_good(
        self, rows: dict[int, Shard], bad: set[int], good: dict[int, bytes], down: set[str], k: int,
        report: ScrubReport,
    ) -> None:
        """Download verified shards until k are in hand (quick mode has none yet)."""
        for index in sorted(rows):
            if len(good) >= k:
                return
            sh = rows[index]
            if index in bad or index in good or sh.provider_id in down:
                continue
            result, data = await self._check_one(sh, True, down)
            if result is Check.OK and data is not None:
                good[index] = data
            elif result in (Check.MISSING, Check.CORRUPT):
                bad.add(index)
                status = ShardStatus.MISSING if result is Check.MISSING else ShardStatus.CORRUPT
                await self.repo.set_shard_statuses({sh.id: status})
                report.missing_found += result is Check.MISSING
                report.corrupt_found += result is Check.CORRUPT

    # --- repairing -------------------------------------------------------------------

    async def _repair_shard(
        self, file_id: uuid.UUID, seg: Segment, rows: dict[int, Shard], bad: set[int], index: int,
        data: bytes, down: set[str], report: ScrubReport,
    ) -> bool:
        row = rows[index]
        if erasure.fingerprint(data) != row.sha256:  # would mean metadata and shards disagree
            log.error("scrub: rebuilt shard does not match its fingerprint seg=%d shard=%d", seg.seg_index, index)
            report.unrepaired += 1
            return False

        holders = Counter(rows[i].provider_id for i in rows if i not in bad and rows[i].provider_id)
        original = row.provider_id
        candidates = [p for p in self.registry if p not in down and holders[p] < self.placer.cap]
        candidates.sort(key=lambda p: (p != original, holders[p]))  # same provider first, then least loaded

        for target in candidates:
            key = row.object_key if target == original else uuid.uuid4().hex
            if target != original:
                # Write-ahead: whatever may exist at the old location stays on record.
                if original is not None:
                    await self.repo.add_orphans([(original, row.object_key)], "repair-moved")
                await self.repo.repoint_shard(row.id, target, key, ShardStatus.PENDING)
                original, row.object_key, row.provider_id = target, key, target
            if await self.placer.put_object(target, key, data, f"repair seg={seg.seg_index} shard={index}"):
                await self.repo.set_shard_statuses({row.id: ShardStatus.STORED})
                bad.discard(index)
                report.repaired += 1
                await self.repo.add_event(
                    "REPAIR",
                    {"file_id": str(file_id), "seg_index": seg.seg_index, "shard_index": index, "provider": target},
                )
                return True
            down.add(target)

        await self.repo.set_shard_statuses({row.id: ShardStatus.MISSING})
        report.unrepaired += 1
        await self.repo.add_event(
            "REPAIR_FAILED",
            {"file_id": str(file_id), "seg_index": seg.seg_index, "shard_index": index, "reason": "no provider"},
        )
        return False


def _rebuild(good: dict[int, bytes], seg: Segment) -> list[bytes]:
    """Verified shards -> segment ciphertext -> all k+m shards again (CPU work, run in a thread)."""
    ciphertext = erasure.decode(good, seg.plain_len + crypto.TAG_SIZE, seg.k, seg.m)
    return erasure.encode(ciphertext, seg.k, seg.m)
