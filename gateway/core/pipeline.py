"""Segment pipeline: chunk -> encrypt -> erasure-code on upload; the reverse on download.

Storage-agnostic: uploads yield shards for the placer, downloads take a `fetch` callable.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import BinaryIO

from cryptography.exceptions import InvalidTag

from gateway.core import crypto, erasure
from gateway.core.chunker import SEGMENT_SIZE, Segment, iter_segments

log = logging.getLogger(__name__)

# fetch(seg_index, shard_index) -> shard bytes; may raise on provider failure.
ShardFetcher = Callable[[int, int], Awaitable[bytes]]
# on_corrupt(seg_index, shard_index), so callers can mark the shard for repair.
CorruptCallback = Callable[[int, int], None]
# rank(seg_index, shard_index): lower is fetched first (e.g. to try known-bad providers last).
ShardRank = Callable[[int, int], int]


class SegmentUnrecoverableError(Exception):
    """No combination of available shards produced an authentic segment."""


@dataclass(frozen=True)
class FileContext:
    file_id: uuid.UUID
    dek: bytes = field(repr=False)
    nonce_prefix: bytes

    @classmethod
    def new(cls, file_id: uuid.UUID | None = None) -> FileContext:
        return cls(
            file_id=file_id or uuid.uuid4(),
            dek=crypto.generate_dek(),
            nonce_prefix=crypto.generate_nonce_prefix(),
        )


@dataclass(frozen=True)
class EncodedShard:
    index: int
    object_key: str  # random UUID: providers never see file names
    sha256: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class EncodedSegment:
    seg_index: int
    plain_len: int
    shards: tuple[EncodedShard, ...]


@dataclass(frozen=True)
class SegmentMeta:
    """What the metadata DB knows about one segment."""

    seg_index: int
    plain_len: int
    shard_sha256: Mapping[int, str]  # shard_index -> hex digest

    @classmethod
    def from_encoded(cls, seg: EncodedSegment) -> SegmentMeta:
        return cls(seg.seg_index, seg.plain_len, {s.index: s.sha256 for s in seg.shards})


@dataclass(frozen=True)
class DecodedSegment:
    plaintext: bytes = field(repr=False)
    corrupt: frozenset[int]  # shard indices that failed the hash check


def encode_segment(
    ctx: FileContext, segment: Segment, k: int = erasure.K, m: int = erasure.M
) -> EncodedSegment:
    """Encrypt FIRST, then erasure-code the ciphertext."""
    ciphertext = crypto.encrypt_segment(
        ctx.dek, ctx.nonce_prefix, ctx.file_id, segment.index, segment.is_last, segment.data
    )
    shards = tuple(
        EncodedShard(index=i, object_key=uuid.uuid4().hex, sha256=erasure.fingerprint(s), data=s)
        for i, s in enumerate(erasure.encode(ciphertext, k, m))
    )
    return EncodedSegment(seg_index=segment.index, plain_len=len(segment.data), shards=shards)


def encode_stream(
    ctx: FileContext,
    stream: BinaryIO,
    segment_size: int = SEGMENT_SIZE,
    k: int = erasure.K,
    m: int = erasure.M,
) -> Iterator[EncodedSegment]:
    for segment in iter_segments(stream, segment_size):
        yield encode_segment(ctx, segment, k, m)


def _hash_ok(ctx: FileContext, meta: SegmentMeta, index: int, data: bytes) -> bool:
    if erasure.fingerprint(data) == meta.shard_sha256.get(index):
        return True
    log.warning("CORRUPT shard file=%s seg=%d shard=%d reason=hash", ctx.file_id, meta.seg_index, index)
    return False


def _decode_verified(
    ctx: FileContext, meta: SegmentMeta, is_last: bool, shards: Mapping[int, bytes], k: int, m: int
) -> bytes:
    """Try k-subsets of hash-verified shards until one decrypts with a valid tag."""
    if len(shards) < k:
        raise erasure.NotEnoughShardsError(
            f"seg {meta.seg_index}: need {k} shards, have {len(shards)}"
        )
    data_len = meta.plain_len + crypto.TAG_SIZE
    for combo in itertools.combinations(sorted(shards), k):
        ciphertext = erasure.decode({i: shards[i] for i in combo}, data_len, k, m)
        try:
            return crypto.decrypt_segment(
                ctx.dek, ctx.nonce_prefix, ctx.file_id, meta.seg_index, is_last, ciphertext
            )
        except InvalidTag:
            log.warning(
                "CORRUPT segment file=%s seg=%d shards=%s reason=tag",
                ctx.file_id, meta.seg_index, combo,
            )
    raise SegmentUnrecoverableError(f"seg {meta.seg_index}: no shard combination authenticates")


def decode_segment(
    ctx: FileContext,
    meta: SegmentMeta,
    is_last: bool,
    shards: Mapping[int, bytes],
    k: int = erasure.K,
    m: int = erasure.M,
) -> DecodedSegment:
    """Verify hashes, erasure-decode, then decrypt, from shards already in hand."""
    valid = {i: d for i, d in shards.items() if _hash_ok(ctx, meta, i, d)}
    corrupt = frozenset(shards) - frozenset(valid)
    return DecodedSegment(_decode_verified(ctx, meta, is_last, valid, k, m), corrupt)


async def fetch_segment(
    ctx: FileContext,
    meta: SegmentMeta,
    is_last: bool,
    fetch: ShardFetcher,
    k: int = erasure.K,
    m: int = erasure.M,
    rank: ShardRank | None = None,
) -> DecodedSegment:
    """Fetch k shards in parallel; on failure or bad hash, fall back to the remaining ones."""
    candidates = sorted(meta.shard_sha256, key=lambda i: (rank(meta.seg_index, i) if rank else 0, i))
    valid: dict[int, bytes] = {}
    corrupt: set[int] = set()
    while len(valid) < k and candidates:
        batch, candidates = candidates[: k - len(valid)], candidates[k - len(valid):]
        results = await asyncio.gather(
            *(fetch(meta.seg_index, i) for i in batch), return_exceptions=True
        )
        for index, result in zip(batch, results):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result  # never swallow cancellation / KeyboardInterrupt
                log.warning(
                    "shard fetch failed file=%s seg=%d shard=%d error=%s",
                    ctx.file_id, meta.seg_index, index, type(result).__name__,
                )
            elif _hash_ok(ctx, meta, index, result):
                valid[index] = result
            else:
                corrupt.add(index)
    return DecodedSegment(_decode_verified(ctx, meta, is_last, valid, k, m), frozenset(corrupt))


async def decode_file(
    ctx: FileContext,
    segments: Sequence[SegmentMeta],
    fetch: ShardFetcher,
    on_corrupt: CorruptCallback | None = None,
    k: int = erasure.K,
    m: int = erasure.M,
    rank: ShardRank | None = None,
) -> AsyncIterator[bytes]:
    """Yield the file's plaintext segment by segment.

    `is_last` comes from the segment's position, never from stored data, so a
    dropped final segment fails authentication instead of silently truncating.
    """
    if not segments:
        raise SegmentUnrecoverableError("file has no segments")
    for position, meta in enumerate(segments):
        if meta.seg_index != position:
            raise SegmentUnrecoverableError(
                f"segment sequence broken at position {position} (got {meta.seg_index})"
            )
        decoded = await fetch_segment(ctx, meta, position == len(segments) - 1, fetch, k, m, rank)
        if on_corrupt:
            for index in sorted(decoded.corrupt):
                on_corrupt(meta.seg_index, index)
        yield decoded.plaintext
