import io

from hypothesis import given
from hypothesis import strategies as st

from gateway.core.chunker import SEGMENT_SIZE, Segment, iter_segments


class TrickleStream(io.RawIOBase):
    """Returns at most `step` bytes per read, like a socket or pipe."""

    def __init__(self, data: bytes, step: int) -> None:
        self._buf = io.BytesIO(data)
        self._step = step

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(min(size, self._step) if size >= 0 else self._step)


def test_default_segment_size_is_4_mib() -> None:
    assert SEGMENT_SIZE == 4 * 1024 * 1024


def test_empty_stream_yields_one_empty_last_segment() -> None:
    assert list(iter_segments(io.BytesIO(b""), 8)) == [Segment(0, b"", True)]


def test_exact_multiple_has_no_trailing_empty_segment() -> None:
    segs = list(iter_segments(io.BytesIO(b"a" * 16), 8))
    assert [(s.index, len(s.data), s.is_last) for s in segs] == [(0, 8, False), (1, 8, True)]


@given(data=st.binary(max_size=2000), size=st.integers(1, 300), step=st.integers(1, 50))
def test_segments_reassemble_and_are_well_formed(data: bytes, size: int, step: int) -> None:
    segs = list(iter_segments(TrickleStream(data, step), size))
    assert b"".join(s.data for s in segs) == data
    assert [s.index for s in segs] == list(range(len(segs)))
    assert [s.is_last for s in segs] == [False] * (len(segs) - 1) + [True]
    assert all(len(s.data) == size for s in segs[:-1])
    assert 0 < len(segs[-1].data) <= size or data == b""
