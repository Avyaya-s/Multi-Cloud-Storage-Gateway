"""SQLAlchemy models. Clouds hold only anonymous shards; this is the only map back to files."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(UTC)


class FileStatus(enum.Enum):
    PENDING = "PENDING"    # upload in progress (write-ahead)
    ACTIVE = "ACTIVE"      # every segment 6/6
    DEGRADED = "DEGRADED"  # every segment >= k+1; repair queued
    FAILED = "FAILED"      # rolled back; leftover rows are for the garbage collector
    DELETED = "DELETED"    # soft-deleted; garbage collector removes shards


class ShardStatus(enum.Enum):
    PENDING = "PENDING"  # row written, upload not confirmed
    STORED = "STORED"
    MISSING = "MISSING"  # upload failed and could not be re-routed
    CORRUPT = "CORRUPT"  # failed hash/tag check on read


class Base(DeclarativeBase):
    pass


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # the provider name
    type: Mapped[str] = mapped_column(String(16))
    endpoint: Mapped[str] = mapped_column(String(255))
    bucket: Mapped[str] = mapped_column(String(255))
    jurisdiction: Mapped[str] = mapped_column(String(8))
    cost_per_gb: Mapped[float] = mapped_column(Float, default=0.0)
    enabled: Mapped[bool] = mapped_column(default=True)


class KekMeta(Base):
    """Argon2id salt/params and a verifier, so a wrong passphrase is caught at startup."""

    __tablename__ = "kek_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    salt: Mapped[bytes] = mapped_column(LargeBinary)
    time_cost: Mapped[int] = mapped_column(Integer)
    memory_cost_kib: Mapped[int] = mapped_column(Integer)
    parallelism: Mapped[int] = mapped_column(Integer)
    verifier: Mapped[bytes] = mapped_column(LargeBinary)  # wrap_dek(kek, random 32 B)


class File(Base):
    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    content_hmac: Mapped[str | None] = mapped_column(String(64))
    policy: Mapped[str] = mapped_column(String(32))
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary)
    nonce_prefix: Mapped[bytes] = mapped_column(LargeBinary)
    segment_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[FileStatus] = mapped_column(Enum(FileStatus, native_enum=False), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    segments: Mapped[list[Segment]] = relationship(
        back_populates="file", cascade="all, delete-orphan", order_by="Segment.seg_index"
    )


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (UniqueConstraint("file_id", "seg_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    seg_index: Mapped[int] = mapped_column(BigInteger)
    plain_len: Mapped[int] = mapped_column(Integer)
    k: Mapped[int] = mapped_column(Integer)
    m: Mapped[int] = mapped_column(Integer)

    file: Mapped[File] = relationship(back_populates="segments")
    shards: Mapped[list[Shard]] = relationship(
        back_populates="segment", cascade="all, delete-orphan", order_by="Shard.shard_index"
    )


class Shard(Base):
    __tablename__ = "shards"
    __table_args__ = (UniqueConstraint("segment_id", "shard_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    segment_id: Mapped[int] = mapped_column(ForeignKey("segments.id", ondelete="CASCADE"), index=True)
    shard_index: Mapped[int] = mapped_column(Integer)
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("providers.id"), index=True)
    object_key: Mapped[str] = mapped_column(String(64), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    status: Mapped[ShardStatus] = mapped_column(Enum(ShardStatus, native_enum=False), index=True)

    segment: Mapped[Segment] = relationship(back_populates="shards")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
