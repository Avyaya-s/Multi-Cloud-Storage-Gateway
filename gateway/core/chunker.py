"""Split a byte stream into fixed-size segments without loading it all into memory."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

SEGMENT_SIZE = 4 * 1024 * 1024  # 4 MiB


@dataclass(frozen=True)
class Segment:
    index: int
    data: bytes
    is_last: bool


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    """Read up to `size` bytes, retrying short reads; returns fewer only at EOF."""
    parts: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def iter_segments(stream: BinaryIO, segment_size: int = SEGMENT_SIZE) -> Iterator[Segment]:
    """Yield consecutive segments of `segment_size` bytes; only the last may be shorter.

    One segment of lookahead is kept so the final segment can be flagged `is_last`.
    An empty stream yields a single empty segment, so every file has >= 1 segment
    and its end is always authenticated.
    """
    if segment_size <= 0:
        raise ValueError("segment_size must be positive")
    index = 0
    current = _read_exact(stream, segment_size)
    while True:
        nxt = _read_exact(stream, segment_size) if len(current) == segment_size else b""
        if not nxt:
            yield Segment(index=index, data=current, is_last=True)
            return
        yield Segment(index=index, data=current, is_last=False)
        index += 1
        current = nxt
