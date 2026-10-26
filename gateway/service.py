"""File operations: wires the pipeline, placer, providers and metadata together."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import BinaryIO

from cryptography.exceptions import InvalidTag

from gateway.core import crypto, erasure, pipeline
from gateway.core.chunker import SEGMENT_SIZE, iter_segments
from gateway.core.pipeline import EncodedSegment, FileContext
from gateway.db.models import File, FileStatus, KekMeta
from gateway.db.repo import Repo, StoredSegment
from gateway.placement import placer as placement
from gateway.placement.placer import Outcome, Placer
from gateway.providers.registry import ProviderRegistry

log = logging.getLogger(__name__)


class UploadFailedError(Exception):
    """Too few shards could be stored; the upload was rolled back."""


class FileNotAvailableError(Exception):
    """The file does not exist or is not in a downloadable state."""


class NotEnoughShardsError(Exception):
    """Some segment has fewer than k readable shards right now."""


async def unlock_kek(repo: Repo, passphrase: str, kdf: crypto.KdfParams) -> bytes:
    """Derive the KEK; create the salt/verifier on first run, verify it afterwards."""
    meta = await repo.get_kek_meta()
    if meta is None:
        salt = crypto.new_salt()
        kek = await asyncio.to_thread(crypto.derive_kek, passphrase, salt, kdf)
        await repo.save_kek_meta(
            KekMeta(
                salt=salt, time_cost=kdf.time_cost, memory_cost_kib=kdf.memory_cost_kib,
                parallelism=kdf.parallelism, verifier=crypto.wrap_dek(kek, os.urandom(crypto.KEY_SIZE)),
            )
        )
        return kek
    params = crypto.KdfParams(meta.time_cost, meta.memory_cost_kib, meta.parallelism)
    kek = await asyncio.to_thread(crypto.derive_kek, passphrase, meta.salt, params)
    try:
        crypto.unwrap_dek(kek, meta.verifier)
    except InvalidTag:
        raise RuntimeError("admin passphrase does not match this metadata database") from None
    return kek


@dataclass
class _UploadState:
    size: int = 0
    segments: int = 0
    degraded: bool = False


class FileService:
    def __init__(
        self,
        repo: Repo,
        registry: ProviderRegistry,
        placer: Placer,
        kek: bytes,
        segment_size: int = SEGMENT_SIZE,
        k: int = erasure.K,
        m: int = erasure.M,
    ) -> None:
        self.repo = repo
        self.registry = registry
        self.placer = placer
        self._kek = kek
        self._hmac_key = crypto.derive_content_hmac_key(kek)
        self.segment_size = segment_size
        self.k = k
        self.m = m

    # --- upload ---------------------------------------------------------------

    async def upload(self, name: str, stream: BinaryIO, policy: str = "standard") -> File:
        ctx = FileContext.new()
        await self.repo.create_file(
            ctx.file_id, name, policy, crypto.wrap_dek(self._kek, ctx.dek), ctx.nonce_prefix
        )
        mac = crypto.new_content_hmac(self._hmac_key)
        segments = iter_segments(stream, self.segment_size)

        def next_encoded() -> EncodedSegment | None:  # CPU + file I/O: runs in a thread
            seg = next(segments, None)
            if seg is None:
                return None
            mac.update(seg.data)
            return pipeline.encode_segment(ctx, seg, self.k, self.m)

        state = _UploadState()
        bad_providers: set[str] = set()
        try:
            while (enc := await asyncio.to_thread(next_encoded)) is not None:
                await self._upload_segment(ctx.file_id, enc, bad_providers, state)
        except Exception as err:
            await self._rollback(ctx.file_id, reason=type(err).__name__)
            if isinstance(err, UploadFailedError):
                raise
            raise UploadFailedError("upload failed; rolled back") from err

        status = FileStatus.DEGRADED if state.degraded else FileStatus.ACTIVE
        await self.repo.finalize_file(ctx.file_id, status, state.size, state.segments, mac.finalize().hex())
        if status is FileStatus.DEGRADED:
            await self.repo.add_event("REPAIR_QUEUED", {"file_id": str(ctx.file_id)})
        await self.repo.add_event("UPLOAD", {"file_id": str(ctx.file_id), "status": status.value})
        file = await self.repo.get_file(ctx.file_id)
        assert file is not None
        return file

    async def _upload_segment(
        self, file_id: uuid.UUID, enc: EncodedSegment, bad_providers: set[str], state: _UploadState
    ) -> None:
        # Providers that already failed during this upload are skipped up front, but only
        # while the rest can still hold every shard: with 3 providers one transient
        # failure must not doom all later segments.
        healthy = [p for p in self.registry if p not in bad_providers]
        enough = len(healthy) * self.placer.cap >= len(enc.shards)
        providers = healthy if enough else list(self.registry)
        assignment = placement.plan(len(enc.shards), providers, self.placer.cap, offset=enc.seg_index)
        await self.repo.add_segment_pending(file_id, enc, assignment, self.k, self.m)

        async def on_reroute(shard_index: int, provider: str) -> None:
            await self.repo.set_shard_provider(file_id, enc.seg_index, shard_index, provider)

        result = await self.placer.execute(enc, assignment, providers, on_reroute)
        bad_providers |= result.failed_providers
        await self.repo.record_placement(file_id, enc.seg_index, result.stored, result.missing)
        for r in result.reroutes:
            await self.repo.add_event(
                "REROUTE",
                {"file_id": str(file_id), "seg_index": enc.seg_index, "shard_index": r.shard_index,
                 "from": r.from_provider, "to": r.to_provider},
            )
        if placement.classify([len(result.stored)], self.k, self.m) is Outcome.ROLLBACK:
            raise UploadFailedError(
                f"segment {enc.seg_index}: only {len(result.stored)}/{len(enc.shards)} shards stored "
                f"(need {self.k + 1}); unavailable providers: {sorted(bad_providers)}"
            )
        state.degraded |= bool(result.missing)
        state.size += enc.plain_len
        state.segments += 1

    async def _rollback(self, file_id: uuid.UUID, reason: str) -> None:
        log.warning("rolling back upload file=%s reason=%s", file_id, reason)
        await self.repo.set_file_status(file_id, FileStatus.FAILED)
        await self._delete_objects(file_id)
        await self.repo.add_event("ROLLBACK", {"file_id": str(file_id), "reason": reason})

    # --- download ------------------------------------------------------------------

    async def open_download(self, file_id: uuid.UUID) -> tuple[File, AsyncIterator[bytes]]:
        """Check the file can be rebuilt, then return a lazy plaintext stream."""
        file = await self.repo.get_file(file_id)
        if file is None or file.status not in (FileStatus.ACTIVE, FileStatus.DEGRADED):
            raise FileNotAvailableError(str(file_id))
        segments = await self.repo.load_stored_segments(file_id)
        if len(segments) != file.segment_count:
            raise FileNotAvailableError(f"{file_id}: segment count does not match metadata")
        short = [s.meta.seg_index for s in segments if len(s.locations) < self.k]
        if short:
            raise NotEnoughShardsError(f"segments {short[:5]} have fewer than {self.k} shards")
        try:
            dek = crypto.unwrap_dek(self._kek, file.wrapped_dek)
        except InvalidTag:
            raise FileNotAvailableError(f"{file_id}: cannot unwrap file key") from None
        ctx = FileContext(file.id, dek, file.nonce_prefix)
        return file, self._stream(ctx, segments)

    async def _stream(self, ctx: FileContext, segments: list[StoredSegment]) -> AsyncIterator[bytes]:
        locations = {s.meta.seg_index: s.locations for s in segments}
        failed_providers: set[str] = set()  # this download only; health scoring comes later

        async def fetch(seg_index: int, shard_index: int) -> bytes:
            provider, key = locations[seg_index][shard_index]
            try:
                return await self.registry[provider].get(key)
            except Exception:
                failed_providers.add(provider)
                raise

        def rank(seg_index: int, shard_index: int) -> int:
            """Try shards on providers that already failed in this download last."""
            return int(locations[seg_index][shard_index][0] in failed_providers)

        corrupt: list[tuple[int, int]] = []
        try:
            async for chunk in pipeline.decode_file(
                ctx, [s.meta for s in segments], fetch, lambda s, i: corrupt.append((s, i)),
                self.k, self.m, rank,
            ):
                yield chunk
        finally:
            if corrupt:
                await self.repo.mark_shards_corrupt(ctx.file_id, corrupt)
                await self.repo.add_event(
                    "CORRUPT", {"file_id": str(ctx.file_id), "shards": [list(c) for c in corrupt]}
                )

    # --- delete ----------------------------------------------------------------

    async def soft_delete(self, file_id: uuid.UUID) -> None:
        file = await self.repo.get_file(file_id)
        if file is None or file.status is FileStatus.DELETED:
            raise FileNotAvailableError(str(file_id))
        await self.repo.set_file_status(file_id, FileStatus.DELETED)
        await self.repo.add_event("DELETE", {"file_id": str(file_id)})

    async def purge(self, file_id: uuid.UUID) -> None:
        """Delete a soft-deleted file's objects now; anything left is for the GC."""
        await self._delete_objects(file_id)

    async def _delete_objects(self, file_id: uuid.UUID) -> None:
        locations = await self.repo.all_shard_locations(file_id)
        failed = await self.placer.delete(locations)
        await self.repo.forget_shards(file_id, [loc for loc in locations if loc not in failed])
