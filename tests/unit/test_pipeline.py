import io
import itertools
import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gateway.core import erasure, pipeline
from gateway.core.pipeline import (
    EncodedSegment,
    FileContext,
    SegmentMeta,
    SegmentUnrecoverableError,
)

SEG = 64  # small segments keep property tests fast


class FakeStore:
    """In-memory shard store keyed by (seg_index, shard_index)."""

    def __init__(self, segments: list[EncodedSegment]) -> None:
        self.blobs = {(s.seg_index, sh.index): sh.data for s in segments for sh in s.shards}
        self.fail: set[tuple[int, int]] = set()
        self.calls: list[tuple[int, int]] = []

    async def fetch(self, seg_index: int, shard_index: int) -> bytes:
        self.calls.append((seg_index, shard_index))
        if (seg_index, shard_index) in self.fail:
            raise ConnectionError("provider down")
        return self.blobs[(seg_index, shard_index)]


def encode(ctx: FileContext, data: bytes) -> list[EncodedSegment]:
    return list(pipeline.encode_stream(ctx, io.BytesIO(data), SEG))


async def download(ctx: FileContext, segs: list[EncodedSegment], store: FakeStore, **kw) -> bytes:
    metas = [SegmentMeta.from_encoded(s) for s in segs]
    return b"".join([p async for p in pipeline.decode_file(ctx, metas, store.fetch, **kw)])


def test_shards_are_ciphertext_with_random_keys() -> None:
    ctx = FileContext.new()
    data = b"TOP SECRET exam results " * 20
    (seg, *_) = encode(ctx, data)
    assert len(seg.shards) == 6
    assert len({sh.object_key for sh in seg.shards}) == 6
    assert all(b"SECRET" not in sh.data for sh in seg.shards)
    assert all(sh.sha256 == erasure.fingerprint(sh.data) for sh in seg.shards)


def test_file_context_repr_hides_dek() -> None:
    ctx = FileContext.new()
    assert ctx.dek.hex() not in repr(ctx) and "dek" not in repr(ctx)


@settings(max_examples=30)
@given(data=st.binary(max_size=SEG * 4))
async def test_w2_round_trip_with_any_two_shards_lost(data: bytes) -> None:
    """W2 milestone, in memory: encrypt -> 6 shards -> delete any 2 -> rebuild -> decrypt."""
    ctx = FileContext.new()
    segs = encode(ctx, data)
    for lost in itertools.combinations(range(6), 2):
        store = FakeStore(segs)
        for s in segs:
            for i in lost:
                del store.blobs[(s.seg_index, i)]
                store.fail.add((s.seg_index, i))
        assert await download(ctx, segs, store) == data


async def test_happy_path_fetches_only_k_shards() -> None:
    ctx = FileContext.new()
    segs = encode(ctx, os.urandom(SEG * 2 + 5))
    store = FakeStore(segs)
    await download(ctx, segs, store)
    assert sorted(store.calls) == [(s, i) for s in range(3) for i in range(4)]


async def test_fallback_to_remaining_shards_on_fetch_failure() -> None:
    ctx = FileContext.new()
    data = os.urandom(SEG + 1)
    segs = encode(ctx, data)
    store = FakeStore(segs)
    store.fail |= {(0, 0), (0, 3)}
    assert await download(ctx, segs, store) == data
    assert {(0, 4), (0, 5)} <= set(store.calls)


async def test_corrupt_shard_detected_reported_and_skipped() -> None:
    ctx = FileContext.new()
    data = os.urandom(SEG * 2)
    segs = encode(ctx, data)
    store = FakeStore(segs)
    blob = bytearray(store.blobs[(1, 2)])
    blob[0] ^= 0xFF
    store.blobs[(1, 2)] = bytes(blob)
    reported: list[tuple[int, int]] = []
    assert await download(ctx, segs, store, on_corrupt=lambda s, i: reported.append((s, i))) == data
    assert reported == [(1, 2)]


async def test_three_lost_shards_is_a_clear_error() -> None:
    ctx = FileContext.new()
    segs = encode(ctx, b"x" * 10)
    store = FakeStore(segs)
    store.fail |= {(0, 0), (0, 2), (0, 5)}
    with pytest.raises(erasure.NotEnoughShardsError):
        await download(ctx, segs, store)


async def test_dropping_last_segment_fails_authentication() -> None:
    ctx = FileContext.new()
    segs = encode(ctx, os.urandom(SEG * 3))  # 3 full segments + nothing (last is full)
    store = FakeStore(segs)
    with pytest.raises(SegmentUnrecoverableError):
        await download(ctx, segs[:-1], store)


async def test_reordered_segments_fail() -> None:
    ctx = FileContext.new()
    segs = encode(ctx, os.urandom(SEG * 2 + 1))
    metas = [SegmentMeta.from_encoded(s) for s in segs]
    swapped = [SegmentMeta(0, metas[1].plain_len, metas[1].shard_sha256),
               SegmentMeta(1, metas[0].plain_len, metas[0].shard_sha256), metas[2]]
    store = FakeStore(segs)
    store.blobs.update({(0, i): segs[1].shards[i].data for i in range(6)})
    store.blobs.update({(1, i): segs[0].shards[i].data for i in range(6)})
    with pytest.raises(SegmentUnrecoverableError):
        _ = [p async for p in pipeline.decode_file(ctx, swapped, store.fetch)]


async def test_gap_in_segment_sequence_rejected() -> None:
    ctx = FileContext.new()
    segs = encode(ctx, os.urandom(SEG * 3))
    store = FakeStore(segs)
    with pytest.raises(SegmentUnrecoverableError):
        await download(ctx, [segs[0], segs[2]], store)


async def test_wrong_dek_fails() -> None:
    ctx = FileContext.new()
    segs = encode(ctx, b"hello")
    wrong = FileContext(ctx.file_id, FileContext.new().dek, ctx.nonce_prefix)
    with pytest.raises(SegmentUnrecoverableError):
        await download(wrong, segs, FakeStore(segs))


def test_decode_segment_sync_reports_corrupt() -> None:
    ctx = FileContext.new()
    (seg,) = encode(ctx, b"abc")
    shards = {sh.index: sh.data for sh in seg.shards}
    shards[0] = b"\x00" * len(shards[0])
    out = pipeline.decode_segment(ctx, SegmentMeta.from_encoded(seg), True, shards)
    assert out.plaintext == b"abc" and out.corrupt == {0}


def test_large_file_streams_in_4_mib_segments() -> None:
    ctx = FileContext.new()
    data = os.urandom(9 * 1024 * 1024)
    segs = list(pipeline.encode_stream(ctx, io.BytesIO(data)))
    assert [s.plain_len for s in segs] == [4 << 20, 4 << 20, 1 << 20]
    rebuilt = b"".join(
        pipeline.decode_segment(
            ctx, SegmentMeta.from_encoded(s), s is segs[-1],
            {sh.index: sh.data for sh in s.shards if sh.index not in (1, 4)},
        ).plaintext
        for s in segs
    )
    assert rebuilt == data
