"""Live demo of what SovereignStripe does today (DPR section 13, steps 1-4).

Needs the compose stack and the API running:
    docker compose -f deploy/docker-compose.yml --env-file .env up -d
    uvicorn gateway.api.routes:app
Then:
    .venv\\Scripts\\python scripts\\demo.py            # runs straight through
    .venv\\Scripts\\python scripts\\demo.py --pause    # waits for Enter between steps
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.providers.registry import ProviderRegistry  # noqa: E402
from gateway.settings import REPO_ROOT, Settings  # noqa: E402

API = "http://127.0.0.1:8000"
COMPOSE = ["docker", "compose", "-f", str(REPO_ROOT / "deploy/docker-compose.yml"),
           "--env-file", str(REPO_ROOT / ".env")]
SECRET = b"CONFIDENTIAL: semester exam results, roll no 1RV24CS057 scored 97/100. "
MIB = 1024 * 1024


def step(n: int, title: str, pause: bool) -> None:
    if pause:
        input("\n[Enter] for next step...")
    print(f"\n{'=' * 70}\n STEP {n}: {title}\n{'=' * 70}")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def show_placement(client: httpx.Client, file_id: str) -> dict:
    placement = client.get(f"/files/{file_id}/placement").json()
    print(f"  {'segment':<9}{'minio-in-1':<14}{'minio-in-2':<14}{'minio-abroad':<14}status")
    for seg in placement["segments"]:
        counts = Counter(s["provider"] for s in seg["shards"])
        statuses = Counter(s["status"] for s in seg["shards"])
        row = "".join(f"{counts.get(p, 0)} shards".ljust(14) for p in ("minio-in-1", "minio-in-2", "minio-abroad"))
        print(f"  {seg['seg_index']:<9}{row}{dict(statuses)}")
    return placement


def download(client: httpx.Client, file_id: str) -> tuple[bytes, float]:
    start = time.perf_counter()
    r = client.get(f"/files/{file_id}/download")
    r.raise_for_status()
    return r.content, time.perf_counter() - start


async def raw_shard(provider: str, key: str) -> bytes:
    reg = ProviderRegistry.from_yaml(REPO_ROOT / "gateway/config/providers.yaml")
    try:
        await reg[provider].open()
        return await reg[provider].get(key)
    finally:
        await reg.close_all()


async def overwrite_shard(provider: str, key: str, data: bytes) -> None:
    reg = ProviderRegistry.from_yaml(REPO_ROOT / "gateway/config/providers.yaml")
    try:
        await reg[provider].open()
        await reg[provider].put(key, data)
    finally:
        await reg.close_all()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pause", action="store_true", help="wait for Enter between steps")
    parser.add_argument("--keep", action="store_true", help="do not delete the demo file at the end")
    args = parser.parse_args()
    Settings.from_env()  # loads .env (MinIO credentials for peeking at raw shards)

    client = httpx.Client(base_url=API, timeout=120)
    try:
        client.get("/files").raise_for_status()
    except httpx.HTTPError:
        sys.exit(f"Gateway not reachable at {API}. Start it with: uvicorn gateway.api.routes:app")

    # A ~10 MB "sensitive" file: readable text, so we can search for it in the cloud.
    original = (SECRET * (10 * MIB // len(SECRET) + 1))[: 10 * MIB]

    step(1, "Upload a 10 MB confidential file", args.pause)
    r = client.post("/files", files={"file": ("exam_results.txt", original)}, data={"policy": "sensitive"})
    r.raise_for_status()
    meta = r.json()
    fid = meta["id"]
    print(f"  file id     : {fid}")
    print(f"  status      : {meta['status']}   segments: {meta['segment_count']} x 4 MB")
    print(f"  sha256      : {sha(original)}...")
    print("\n  Where the shards went (RS(4,2): 6 shards per segment, max 2 per cloud):")
    placement = show_placement(client, fid)

    step(2, "Look at what a cloud actually stores", args.pause)
    shard = placement["segments"][0]["shards"][0]
    raw = asyncio.run(raw_shard(shard["provider"], shard["object_key"]))
    print(f"  provider    : {shard['provider']}")
    print(f"  object name : {shard['object_key']}   (random UUID, no file name)")
    print(f"  size        : {len(raw):,} bytes  (1/4 of an encrypted 4 MB segment)")
    print(f"  first bytes : {raw[:32].hex(' ')}")
    print(f"  contains 'CONFIDENTIAL'? {b'CONFIDENTIAL' in raw}   <- ciphertext only")

    step(3, "Normal download", args.pause)
    data, secs = download(client, fid)
    print(f"  downloaded {len(data):,} bytes in {secs:.2f}s, sha256 {sha(data)}...")
    print(f"  identical to original? {data == original}")

    step(4, "Kill a whole cloud: docker stop minio-abroad", args.pause)
    subprocess.run([*COMPOSE, "stop", "minio-abroad"], check=True, capture_output=True)
    print("  minio-abroad is DOWN (2 of every 6 shards unreachable)")
    try:
        data, secs = download(client, fid)
        print(f"  downloaded {len(data):,} bytes in {secs:.2f}s, sha256 {sha(data)}...")
        print(f"  identical to original? {data == original}   <- rebuilt from the other 4 shards")

        print("\n  Uploading a NEW file while a cloud is down (only 4/6 shards can be stored):")
        r = client.post("/files", files={"file": ("new.txt", b"x" * MIB)})
        print(f"  HTTP {r.status_code}: {r.json()['detail']}")
        print("  -> rolled back instead of storing an under-protected file")
    finally:
        step(5, "Bring the cloud back: docker start minio-abroad", args.pause)
        subprocess.run([*COMPOSE, "start", "minio-abroad"], check=True, capture_output=True)
        time.sleep(3)
        print("  minio-abroad is UP again")

    step(6, "Tamper with a stored shard (simulated bit rot / malicious cloud)", args.pause)
    victim = placement["segments"][1]["shards"][2]
    asyncio.run(overwrite_shard(victim["provider"], victim["object_key"], os.urandom(victim["size"])))
    print(f"  overwrote segment 1, shard 2 on {victim['provider']} with random bytes")
    data, secs = download(client, fid)
    print(f"  downloaded in {secs:.2f}s; identical to original? {data == original}")
    print("  SHA-256 check rejected the bad shard and another one was used instead:")
    show_placement(client, fid)
    print("  (the scrubber, next phase, will rebuild that CORRUPT shard automatically)")

    if not args.keep:
        step(7, "Delete the file", args.pause)
        client.delete(f"/files/{fid}").raise_for_status()
        time.sleep(1)
        print(f"  files listed now: {len(client.get('/files').json())}; shards removed from all clouds")

    print("\nDemo complete.")


if __name__ == "__main__":
    main()
