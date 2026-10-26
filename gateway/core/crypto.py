"""Envelope encryption: per-file DEK (AES-256-GCM per segment), wrapped by a passphrase KEK.

Only primitives from `cryptography` and `argon2-cffi` are used. Keys are plain `bytes`
and must never be logged, printed or returned from an API.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_SIZE = 32  # AES-256
NONCE_PREFIX_SIZE = 4
SEG_INDEX_SIZE = 8
NONCE_SIZE = NONCE_PREFIX_SIZE + SEG_INDEX_SIZE  # 12, the GCM standard
TAG_SIZE = 16
SALT_SIZE = 16
MAX_SEG_INDEX = 2**64 - 1

DEK_WRAP_AAD = b"dek-wrap"
CONTENT_HMAC_INFO = b"content-hmac"


@dataclass(frozen=True)
class KdfParams:
    """Argon2id cost parameters (defaults: RFC 9106 second recommended option)."""

    time_cost: int = 3
    memory_cost_kib: int = 64 * 1024
    parallelism: int = 4


def new_salt() -> bytes:
    return os.urandom(SALT_SIZE)


def derive_kek(passphrase: str, salt: bytes, params: KdfParams = KdfParams()) -> bytes:
    """Derive the 256-bit KEK from the admin passphrase with Argon2id."""
    if len(salt) < SALT_SIZE:
        raise ValueError(f"salt must be at least {SALT_SIZE} bytes")
    return hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=params.time_cost,
        memory_cost=params.memory_cost_kib,
        parallelism=params.parallelism,
        hash_len=KEY_SIZE,
        type=Type.ID,
    )


def generate_dek() -> bytes:
    return AESGCM.generate_key(bit_length=KEY_SIZE * 8)


def generate_nonce_prefix() -> bytes:
    return os.urandom(NONCE_PREFIX_SIZE)


def _seg_index_bytes(seg_index: int) -> bytes:
    if not 0 <= seg_index <= MAX_SEG_INDEX:
        raise ValueError("seg_index out of range")
    return seg_index.to_bytes(SEG_INDEX_SIZE, "big")


def segment_nonce(nonce_prefix: bytes, seg_index: int) -> bytes:
    """nonce = prefix (4 B) || seg_index (8 B big-endian)."""
    if len(nonce_prefix) != NONCE_PREFIX_SIZE:
        raise ValueError(f"nonce_prefix must be {NONCE_PREFIX_SIZE} bytes")
    return nonce_prefix + _seg_index_bytes(seg_index)


def segment_aad(file_id: uuid.UUID, seg_index: int, is_last: bool) -> bytes:
    """AAD = file_id (16 B) || seg_index (8 B) || is_last (1 B)."""
    return file_id.bytes + _seg_index_bytes(seg_index) + (b"\x01" if is_last else b"\x00")


def encrypt_segment(
    dek: bytes,
    nonce_prefix: bytes,
    file_id: uuid.UUID,
    seg_index: int,
    is_last: bool,
    plaintext: bytes,
) -> bytes:
    """Return ciphertext || 16-byte tag."""
    return AESGCM(dek).encrypt(
        segment_nonce(nonce_prefix, seg_index),
        plaintext,
        segment_aad(file_id, seg_index, is_last),
    )


def decrypt_segment(
    dek: bytes,
    nonce_prefix: bytes,
    file_id: uuid.UUID,
    seg_index: int,
    is_last: bool,
    ciphertext: bytes,
) -> bytes:
    """Raise `cryptography.exceptions.InvalidTag` if anything was tampered with."""
    return AESGCM(dek).decrypt(
        segment_nonce(nonce_prefix, seg_index),
        ciphertext,
        segment_aad(file_id, seg_index, is_last),
    )


def wrap_dek(kek: bytes, dek: bytes) -> bytes:
    """Return random nonce (12 B) || AES-GCM(kek, dek, aad=b"dek-wrap")."""
    nonce = os.urandom(NONCE_SIZE)
    return nonce + AESGCM(kek).encrypt(nonce, dek, DEK_WRAP_AAD)


def unwrap_dek(kek: bytes, wrapped: bytes) -> bytes:
    """Raise `InvalidTag` on a wrong KEK or a modified wrapped key."""
    nonce, body = wrapped[:NONCE_SIZE], wrapped[NONCE_SIZE:]
    return AESGCM(kek).decrypt(nonce, body, DEK_WRAP_AAD)


def derive_content_hmac_key(kek: bytes) -> bytes:
    """HKDF-SHA256(kek, info=b"content-hmac"); the KEK itself is never used for HMAC."""
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_SIZE, salt=None, info=CONTENT_HMAC_INFO
    ).derive(kek)


def new_content_hmac(hmac_key: bytes) -> hmac.HMAC:
    """Incremental HMAC-SHA256 over a file's plaintext (call .update per segment)."""
    return hmac.HMAC(hmac_key, hashes.SHA256())
