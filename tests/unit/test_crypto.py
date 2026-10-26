import uuid

import pytest
from cryptography.exceptions import InvalidTag
from hypothesis import given
from hypothesis import strategies as st

from gateway.core import crypto

FAST_KDF = crypto.KdfParams(time_cost=1, memory_cost_kib=8, parallelism=1)

keys = st.binary(min_size=32, max_size=32)
prefixes = st.binary(min_size=4, max_size=4)
uuids = st.uuids()
seg_indices = st.integers(0, crypto.MAX_SEG_INDEX)


def test_nonce_layout() -> None:
    assert crypto.segment_nonce(b"\xaa\xbb\xcc\xdd", 258) == b"\xaa\xbb\xcc\xdd" + (258).to_bytes(8, "big")


def test_aad_layout() -> None:
    fid = uuid.uuid4()
    assert crypto.segment_aad(fid, 1, True) == fid.bytes + (1).to_bytes(8, "big") + b"\x01"
    assert crypto.segment_aad(fid, 1, False)[-1:] == b"\x00"


@pytest.mark.parametrize("bad", [-1, 2**64])
def test_seg_index_out_of_range(bad: int) -> None:
    with pytest.raises(ValueError):
        crypto.segment_nonce(b"\x00" * 4, bad)


def test_nonce_prefix_length_enforced() -> None:
    with pytest.raises(ValueError):
        crypto.segment_nonce(b"\x00" * 3, 0)


@given(dek=keys, prefix=prefixes, fid=uuids, idx=seg_indices, last=st.booleans(), pt=st.binary(max_size=512))
def test_segment_round_trip(dek: bytes, prefix: bytes, fid: uuid.UUID, idx: int, last: bool, pt: bytes) -> None:
    ct = crypto.encrypt_segment(dek, prefix, fid, idx, last, pt)
    assert len(ct) == len(pt) + crypto.TAG_SIZE
    assert crypto.decrypt_segment(dek, prefix, fid, idx, last, ct) == pt


@given(dek=keys, prefix=prefixes, fid=uuids, pt=st.binary(max_size=256), data=st.data())
def test_any_bit_flip_is_rejected(dek: bytes, prefix: bytes, fid: uuid.UUID, pt: bytes, data: st.DataObject) -> None:
    ct = bytearray(crypto.encrypt_segment(dek, prefix, fid, 0, True, pt))
    pos = data.draw(st.integers(0, len(ct) - 1))
    ct[pos] ^= 1 << data.draw(st.integers(0, 7))
    with pytest.raises(InvalidTag):
        crypto.decrypt_segment(dek, prefix, fid, 0, True, bytes(ct))


@given(dek=keys, prefix=prefixes, fid=uuids, pt=st.binary(max_size=64))
def test_context_binding(dek: bytes, prefix: bytes, fid: uuid.UUID, pt: bytes) -> None:
    ct = crypto.encrypt_segment(dek, prefix, fid, 5, False, pt)
    wrong_contexts = [
        (fid, 6, False),          # reordered segment
        (fid, 5, True),           # truncation: claimed to be last
        (uuid.uuid4(), 5, False), # segment moved to another file
    ]
    for other_fid, idx, last in wrong_contexts:
        with pytest.raises(InvalidTag):
            crypto.decrypt_segment(dek, prefix, other_fid, idx, last, ct)


@given(kek=keys, dek=keys)
def test_wrap_round_trip(kek: bytes, dek: bytes) -> None:
    wrapped = crypto.wrap_dek(kek, dek)
    assert dek not in wrapped
    assert crypto.unwrap_dek(kek, wrapped) == dek


def test_wrap_uses_fresh_nonce() -> None:
    kek, dek = crypto.generate_dek(), crypto.generate_dek()
    assert crypto.wrap_dek(kek, dek) != crypto.wrap_dek(kek, dek)


def test_unwrap_with_wrong_kek_fails() -> None:
    wrapped = crypto.wrap_dek(crypto.generate_dek(), crypto.generate_dek())
    with pytest.raises(InvalidTag):
        crypto.unwrap_dek(crypto.generate_dek(), wrapped)


def test_derive_kek_is_deterministic_per_salt() -> None:
    salt = crypto.new_salt()
    kek = crypto.derive_kek("correct horse", salt, FAST_KDF)
    assert len(kek) == crypto.KEY_SIZE
    assert kek == crypto.derive_kek("correct horse", salt, FAST_KDF)
    assert kek != crypto.derive_kek("correct horse", crypto.new_salt(), FAST_KDF)
    assert kek != crypto.derive_kek("wrong horse", salt, FAST_KDF)


def test_derive_kek_rejects_short_salt() -> None:
    with pytest.raises(ValueError):
        crypto.derive_kek("pw", b"short", FAST_KDF)


def test_content_hmac_key_is_derived_not_the_kek() -> None:
    kek = crypto.generate_dek()
    hkey = crypto.derive_content_hmac_key(kek)
    assert len(hkey) == crypto.KEY_SIZE
    assert hkey != kek
    assert hkey == crypto.derive_content_hmac_key(kek)
    assert hkey != crypto.derive_content_hmac_key(crypto.generate_dek())


def test_content_hmac_is_incremental() -> None:
    hkey = crypto.derive_content_hmac_key(crypto.generate_dek())
    whole = crypto.new_content_hmac(hkey)
    whole.update(b"abcdef")
    parts = crypto.new_content_hmac(hkey)
    parts.update(b"abc")
    parts.update(b"def")
    assert whole.finalize() == parts.finalize()
