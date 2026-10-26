import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gateway.core import erasure
from gateway.core.erasure import K, M, NotEnoughShardsError

ALL_K_SUBSETS = list(itertools.combinations(range(K + M), K))


def test_rs_4_2_shape() -> None:
    shards = erasure.encode(b"x" * 100)
    assert len(shards) == 6
    assert {len(s) for s in shards} == {25}  # 1.5x overhead: 6 * 25 = 150


@given(data=st.binary(max_size=1000))
def test_every_k_of_n_subset_rebuilds(data: bytes) -> None:
    shards = erasure.encode(data)
    for subset in ALL_K_SUBSETS:
        assert erasure.decode({i: shards[i] for i in subset}, len(data)) == data


@given(data=st.binary(max_size=300), k=st.integers(1, 8), m=st.integers(0, 4), seed=st.randoms())
def test_other_rs_parameters(data: bytes, k: int, m: int, seed) -> None:
    shards = erasure.encode(data, k, m)
    keep = seed.sample(range(k + m), k)
    assert erasure.decode({i: shards[i] for i in keep}, len(data), k, m) == data


def test_too_few_shards() -> None:
    shards = erasure.encode(b"hello world")
    with pytest.raises(NotEnoughShardsError):
        erasure.decode({i: shards[i] for i in range(3)}, 11)


def test_mismatched_lengths_rejected() -> None:
    shards = erasure.encode(b"hello world")
    bad = {0: shards[0], 1: shards[1] + b"x", 2: shards[2], 3: shards[3]}
    with pytest.raises(ValueError):
        erasure.decode(bad, 11)


def test_wrong_data_len_rejected() -> None:
    shards = erasure.encode(b"hello world")
    with pytest.raises(ValueError):
        erasure.decode(dict(enumerate(shards)), 100)


def test_out_of_range_index_rejected() -> None:
    shards = erasure.encode(b"hello world")
    with pytest.raises(ValueError):
        erasure.decode({0: shards[0], 1: shards[1], 2: shards[2], 9: shards[3]}, 11)


def test_fingerprint_is_sha256_hex() -> None:
    assert erasure.fingerprint(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
