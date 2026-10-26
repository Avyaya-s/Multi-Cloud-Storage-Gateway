"""Async metadata repository. Each method is one short transaction."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from gateway.core.pipeline import EncodedSegment, SegmentMeta
from gateway.db.models import (
    Base,
    Event,
    File,
    FileStatus,
    KekMeta,
    Provider,
    Segment,
    Shard,
    ShardStatus,
)
from gateway.providers.registry import ProviderConfig


@dataclass(frozen=True)
class StoredSegment:
    """A segment's metadata plus where each readable shard lives."""

    meta: SegmentMeta
    locations: Mapping[int, tuple[str, str]]  # shard_index -> (provider, object_key)


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


class Repo:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        return self._session()

    async def create_schema(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # --- providers / keys -------------------------------------------------------

    async def sync_providers(self, configs: Iterable[ProviderConfig]) -> None:
        async with self.session() as s, s.begin():
            for c in configs:
                await s.merge(
                    Provider(
                        id=c.name, type=c.type, endpoint=c.endpoint, bucket=c.bucket,
                        jurisdiction=c.jurisdiction, cost_per_gb=c.cost_per_gb, enabled=c.enabled,
                    )
                )

    async def get_kek_meta(self) -> KekMeta | None:
        async with self.session() as s:
            return await s.get(KekMeta, 1)

    async def save_kek_meta(self, meta: KekMeta) -> None:
        async with self.session() as s, s.begin():
            meta.id = 1
            s.add(meta)

    # --- upload (write-ahead) ------------------------------------------------------

    async def create_file(
        self, file_id: uuid.UUID, name: str, policy: str, wrapped_dek: bytes, nonce_prefix: bytes
    ) -> None:
        async with self.session() as s, s.begin():
            s.add(
                File(
                    id=file_id, name=name, policy=policy, wrapped_dek=wrapped_dek,
                    nonce_prefix=nonce_prefix, status=FileStatus.PENDING,
                )
            )

    async def add_segment_pending(
        self, file_id: uuid.UUID, seg: EncodedSegment, assignment: Sequence[str | None], k: int, m: int
    ) -> None:
        """Insert the segment and all its shard rows as PENDING before any upload starts."""
        async with self.session() as s, s.begin():
            row = Segment(file_id=file_id, seg_index=seg.seg_index, plain_len=seg.plain_len, k=k, m=m)
            row.shards = [
                Shard(
                    shard_index=sh.index, provider_id=provider, object_key=sh.object_key,
                    sha256=sh.sha256, size=len(sh.data), status=ShardStatus.PENDING,
                )
                for sh, provider in zip(seg.shards, assignment)
            ]
            s.add(row)

    async def set_shard_provider(self, file_id: uuid.UUID, seg_index: int, shard_index: int, provider: str) -> None:
        async with self.session() as s, s.begin():
            await s.execute(
                update(Shard)
                .where(Shard.segment_id == self._segment_id(file_id, seg_index), Shard.shard_index == shard_index)
                .values(provider_id=provider)
            )

    async def record_placement(
        self, file_id: uuid.UUID, seg_index: int, stored: Mapping[int, str], missing: Iterable[int]
    ) -> None:
        seg_id = self._segment_id(file_id, seg_index)
        async with self.session() as s, s.begin():
            for index, provider in stored.items():
                await s.execute(
                    update(Shard)
                    .where(Shard.segment_id == seg_id, Shard.shard_index == index)
                    .values(provider_id=provider, status=ShardStatus.STORED)
                )
            missing = list(missing)
            if missing:
                await s.execute(
                    update(Shard)
                    .where(Shard.segment_id == seg_id, Shard.shard_index.in_(missing))
                    .values(status=ShardStatus.MISSING)
                )

    async def finalize_file(
        self, file_id: uuid.UUID, status: FileStatus, size: int, segment_count: int, content_hmac: str
    ) -> None:
        async with self.session() as s, s.begin():
            await s.execute(
                update(File)
                .where(File.id == file_id)
                .values(status=status, size=size, segment_count=segment_count, content_hmac=content_hmac)
            )

    @staticmethod
    def _segment_id(file_id: uuid.UUID, seg_index: int) -> Any:
        return (
            select(Segment.id)
            .where(Segment.file_id == file_id, Segment.seg_index == seg_index)
            .scalar_subquery()
        )

    # --- reads ----------------------------------------------------------------

    async def get_file(self, file_id: uuid.UUID) -> File | None:
        async with self.session() as s:
            return await s.get(File, file_id)

    async def list_files(self, include_deleted: bool = False) -> list[File]:
        stmt = select(File).order_by(File.created_at.desc())
        if not include_deleted:
            stmt = stmt.where(File.status.in_([FileStatus.ACTIVE, FileStatus.DEGRADED, FileStatus.PENDING]))
        async with self.session() as s:
            return list((await s.scalars(stmt)).all())

    async def get_placement(self, file_id: uuid.UUID) -> list[Segment]:
        async with self.session() as s:
            stmt = (
                select(Segment)
                .where(Segment.file_id == file_id)
                .options(selectinload(Segment.shards))
                .order_by(Segment.seg_index)
            )
            return list((await s.scalars(stmt)).all())

    async def load_stored_segments(self, file_id: uuid.UUID) -> list[StoredSegment]:
        """Segments in order, with only STORED shards (others are treated as missing)."""
        out = []
        for seg in await self.get_placement(file_id):
            good = [sh for sh in seg.shards if sh.status == ShardStatus.STORED and sh.provider_id]
            out.append(
                StoredSegment(
                    meta=SegmentMeta(seg.seg_index, seg.plain_len, {sh.shard_index: sh.sha256 for sh in good}),
                    locations={sh.shard_index: (sh.provider_id, sh.object_key) for sh in good},  # type: ignore[misc]
                )
            )
        return out

    async def all_shard_locations(self, file_id: uuid.UUID) -> list[tuple[str, str]]:
        """Every (provider, object_key) an object of this file may exist on, any status."""
        async with self.session() as s:
            rows = await s.execute(
                select(Shard.provider_id, Shard.object_key)
                .join(Segment)
                .where(Segment.file_id == file_id, Shard.provider_id.is_not(None))
            )
            return [(p, k) for p, k in rows.all()]

    # --- state changes ------------------------------------------------------------

    async def mark_shards_corrupt(self, file_id: uuid.UUID, shards: Iterable[tuple[int, int]]) -> None:
        async with self.session() as s, s.begin():
            for seg_index, shard_index in shards:
                await s.execute(
                    update(Shard)
                    .where(Shard.segment_id == self._segment_id(file_id, seg_index), Shard.shard_index == shard_index)
                    .values(status=ShardStatus.CORRUPT)
                )

    async def set_file_status(self, file_id: uuid.UUID, status: FileStatus) -> None:
        async with self.session() as s, s.begin():
            await s.execute(update(File).where(File.id == file_id).values(status=status))

    async def forget_shards(self, file_id: uuid.UUID, removed: Iterable[tuple[str, str]]) -> None:
        """Drop rows for objects confirmed deleted; drop the file once nothing is left."""
        keys = [k for _, k in removed]
        async with self.session() as s, s.begin():
            if keys:
                await s.execute(delete(Shard).where(Shard.object_key.in_(keys)))
            remaining = await s.scalar(
                select(Shard.id).join(Segment).where(Segment.file_id == file_id).limit(1)
            )
            if remaining is None:
                await s.execute(delete(Segment).where(Segment.file_id == file_id))
                await s.execute(delete(File).where(File.id == file_id))

    async def add_event(self, type_: str, details: dict[str, Any]) -> None:
        async with self.session() as s, s.begin():
            s.add(Event(type=type_, details_json=details))

    async def events(self, type_: str | None = None) -> list[Event]:
        stmt = select(Event).order_by(Event.id)
        if type_:
            stmt = stmt.where(Event.type == type_)
        async with self.session() as s:
            return list((await s.scalars(stmt)).all())
