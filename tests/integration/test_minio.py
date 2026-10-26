"""End-to-end tests on real MinIO containers and Postgres (see conftest for setup)."""

from __future__ import annotations

import os
from collections import Counter

import pytest

from tests.integration.conftest import compose, stack_up, wait_ready

pytestmark = pytest.mark.skipif(not stack_up(), reason="docker compose stack not running")

THREE = ["minio-in-1", "minio-in-2", "minio-abroad"]
MIB = 1024 * 1024


def assert_spread(placement: dict, providers: list[str]) -> None:
    for seg in placement["segments"]:
        assert [s["status"] for s in seg["shards"]] == ["STORED"] * 6
        counts = Counter(s["provider"] for s in seg["shards"])
        assert set(counts) <= set(providers) and max(counts.values()) <= 2


async def test_a_upload_download_byte_for_byte(gateway) -> None:
    client, registry = await gateway(THREE)
    marker = b"PLAINTEXT-MARKER-" * 1000
    data = marker + os.urandom(9 * MIB)  # 3 segments of 4 MiB
    r = await client.post("/files", files={"file": ("big.bin", data)})
    assert r.status_code == 201, r.text
    meta = r.json()
    assert meta["status"] == "ACTIVE" and meta["segment_count"] == 3

    placement = (await client.get(f"/files/{meta['id']}/placement")).json()
    assert_spread(placement, THREE)
    # What a cloud actually holds: random-named ciphertext, never the plaintext.
    first = placement["segments"][0]["shards"][0]
    raw = await registry[first["provider"]].get(first["object_key"])
    assert b"PLAINTEXT-MARKER" not in raw and "big" not in first["object_key"]

    r = await client.get(f"/files/{meta['id']}/download")
    assert r.status_code == 200 and r.content == data


async def test_b_download_with_one_minio_stopped(gateway) -> None:
    client, _ = await gateway(THREE)
    data = os.urandom(6 * MIB + 123)
    meta = (await client.post("/files", files={"file": ("f.bin", data)})).json()
    assert meta["status"] == "ACTIVE"

    compose("stop", "minio-abroad")
    try:
        r = await client.get(f"/files/{meta['id']}/download")
        assert r.status_code == 200 and r.content == data

        # Uploading with a whole provider down leaves only 4/6 shards: roll back, 503.
        r = await client.post("/files", files={"file": ("g.bin", os.urandom(MIB))})
        assert r.status_code == 503 and "only 4/6" in r.json()["detail"]
        assert [f["id"] for f in (await client.get("/files")).json()] == [meta["id"]]
    finally:
        compose("start", "minio-abroad")
        await wait_ready("minio-abroad")


@pytest.fixture
async def extra_minio():
    compose("--profile", "extra", "up", "-d", "minio-extra")
    await wait_ready("minio-extra")
    yield
    compose("--profile", "extra", "stop", "minio-extra")


async def test_c_four_providers_failing_shard_is_rerouted(gateway, extra_minio) -> None:
    four = [*THREE, "minio-extra"]
    client, _ = await gateway(four)  # opens all 4 (creates the bucket on minio-extra)
    compose("stop", "minio-extra")   # ...then that provider fails mid-life

    data = os.urandom(9 * MIB)
    r = await client.post("/files", files={"file": ("r.bin", data)})
    assert r.status_code == 201, r.text
    meta = r.json()
    assert meta["status"] == "ACTIVE"  # every shard found a home despite the failure

    placement = (await client.get(f"/files/{meta['id']}/placement")).json()
    assert_spread(placement, THREE)  # nothing ended up on minio-extra, cap of 2 respected

    service = client._transport.app.state.service  # type: ignore[attr-defined]
    reroutes = await service.repo.events("REROUTE")
    assert reroutes, "expected at least one re-routed shard"
    assert {e.details_json["from"] for e in reroutes} == {"minio-extra"}
    assert {e.details_json["to"] for e in reroutes} <= set(THREE)

    r = await client.get(f"/files/{meta['id']}/download")
    assert r.status_code == 200 and r.content == data
