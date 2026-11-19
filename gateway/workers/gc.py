"""Garbage collector: delete cloud objects that no live file needs any more.

Where garbage comes from (all recorded write-ahead, so nothing is lost track of):
1. orphans table   - old copies left behind when the scrubber moved a shard elsewhere
2. FAILED files    - uploads that rolled back while a provider was unreachable
3. DELETED files   - deletes whose background purge could not reach every cloud
4. stale PENDING   - uploads interrupted by a crash, older than a grace period
5. sweep (opt-in)  - objects in a bucket that NO metadata row mentions at all (crash
                     windows, the delete-during-repair race), older than a grace period

Every step is safe to repeat: deleting a key that no longer exists is not an error,
and metadata rows are removed only after the provider confirmed the delete. An
unreachable provider simply keeps its rows until a later run.

The sweep assumes each bucket belongs to this gateway alone: two gateways with
different databases sharing one bucket would delete each other's shards.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from gateway.db.models import FileStatus
from gateway.db.repo import Repo
from gateway.placement.placer import Placer
from gateway.providers.base import ProviderError
from gateway.providers.registry import ProviderRegistry

log = logging.getLogger(__name__)


class GcBusyError(Exception):
    """A garbage collection is already running."""


@dataclass
class GcReport:
    sweep: bool
    orphans_deleted: int = 0
    orphans_kept: int = 0  # provider unreachable: retried next run
    stale_uploads: int = 0  # PENDING uploads given up on (marked FAILED)
    objects_deleted: int = 0  # shards of FAILED/DELETED files
    objects_kept: int = 0
    files_removed: int = 0  # dead files whose metadata is now completely gone
    swept: int = 0  # untracked objects deleted by the sweep
    sweep_skipped: list[str] = field(default_factory=list)  # providers that could not be listed
    duration_s: float = 0.0


def _aware(ts: datetime) -> datetime:
    """SQLite returns naive datetimes; everything we store is UTC."""
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


class GarbageCollector:
    def __init__(
        self,
        repo: Repo,
        registry: ProviderRegistry,
        placer: Placer,
        pending_grace_s: float = 3600,
        sweep_min_age_s: float = 3600,
    ) -> None:
        self.repo = repo
        self.registry = registry
        self.placer = placer
        self.pending_grace = timedelta(seconds=pending_grace_s)
        self.sweep_min_age = timedelta(seconds=sweep_min_age_s)
        self._lock = asyncio.Lock()

    async def run(self, sweep: bool = False) -> GcReport:
        if self._lock.locked():
            raise GcBusyError("garbage collection is already running")
        async with self._lock:
            report = GcReport(sweep=sweep)
            start = time.perf_counter()
            await self._collect_orphans(report)
            await self._fail_stale_uploads(report)
            await self._purge_dead_files(report)
            if sweep:
                await self._sweep(report)
            report.duration_s = round(time.perf_counter() - start, 3)
            await self.repo.add_event("GC", asdict(report))
            log.info("gc done %s", report)
            return report

    async def _collect_orphans(self, report: GcReport) -> None:
        orphans = await self.repo.orphans()
        failed = await self.placer.delete([(o.provider_id, o.object_key) for o in orphans])
        done = [o.id for o in orphans if (o.provider_id, o.object_key) not in failed]
        await self.repo.remove_orphans(done)
        report.orphans_deleted += len(done)
        report.orphans_kept += len(orphans) - len(done)

    async def _fail_stale_uploads(self, report: GcReport) -> None:
        """An upload still PENDING long after it started was interrupted (crash, kill -9)."""
        cutoff = datetime.now(UTC) - self.pending_grace
        for f in await self.repo.files_with_status(FileStatus.PENDING):
            if _aware(f.created_at) < cutoff:
                await self.repo.set_file_status(f.id, FileStatus.FAILED)
                await self.repo.add_event("STALE_UPLOAD", {"file_id": str(f.id)})
                report.stale_uploads += 1

    async def _purge_dead_files(self, report: GcReport) -> None:
        for f in await self.repo.files_with_status(FileStatus.FAILED, FileStatus.DELETED):
            locations = await self.repo.all_shard_locations(f.id)
            failed = await self.placer.delete(locations)
            await self.repo.forget_shards(f.id, [loc for loc in locations if loc not in failed])
            report.objects_deleted += len(locations) - len(failed)
            report.objects_kept += len(failed)
            if await self.repo.get_file(f.id) is None:
                report.files_removed += 1

    async def _sweep(self, report: GcReport) -> None:
        """Delete objects no metadata row knows about. List FIRST, then read the known keys:
        metadata is always written before an object (write-ahead), so anything listed is
        either already known or truly untracked."""
        cutoff = datetime.now(UTC) - self.sweep_min_age
        for name, provider in self.registry.items():
            try:
                listing = await provider.list_objects()
            except (ProviderError, NotImplementedError) as err:
                log.warning("sweep: cannot list %s (%s)", name, type(err).__name__)
                report.sweep_skipped.append(name)
                continue
            known = await self.repo.known_keys(name)
            untracked = [o.key for o in listing if o.key not in known and _aware(o.last_modified) < cutoff]
            failed = await self.placer.delete([(name, key) for key in untracked])
            report.swept += len(untracked) - len(failed)
            if untracked:
                log.warning("sweep: %s had %d untracked objects", name, len(untracked))
