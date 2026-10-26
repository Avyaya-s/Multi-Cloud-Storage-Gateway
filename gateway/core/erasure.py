"""Reed-Solomon RS(k, m) erasure coding via zfec.easyfec.

Our (k, m) means k data + m parity shards; zfec's own "m" is the total, k + m.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from zfec.easyfec import Decoder, Encoder

K = 4
M = 2


class NotEnoughShardsError(Exception):
    """Fewer than k usable shards are available for a segment."""


def encode(data: bytes, k: int = K, m: int = M) -> list[bytes]:
    """Split `data` into k + m equal-length shards; any k of them rebuild it."""
    return list(Encoder(k, k + m).encode(data))


def decode(shards: Mapping[int, bytes], data_len: int, k: int = K, m: int = M) -> bytes:
    """Rebuild the original `data_len` bytes from any k shards {shard_index: bytes}."""
    if len(shards) < k:
        raise NotEnoughShardsError(f"need {k} shards, have {len(shards)}")
    # Prefer the lowest indices: data shards decode without any GF(256) work.
    chosen = sorted(shards)[:k]
    if any(not 0 <= i < k + m for i in chosen):
        raise ValueError("shard index out of range")
    blocks = [shards[i] for i in chosen]
    block_len = len(blocks[0])
    if any(len(b) != block_len for b in blocks):
        raise ValueError("shards have different lengths")
    padlen = block_len * k - data_len
    if not 0 <= padlen < k:
        raise ValueError("data_len does not match shard size")
    return Decoder(k, k + m).decode(blocks, chosen, padlen)


def fingerprint(shard: bytes) -> str:
    """SHA-256 hex digest stored in metadata and checked on every read."""
    return hashlib.sha256(shard).hexdigest()
