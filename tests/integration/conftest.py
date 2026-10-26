"""Fixtures for tests against the Docker Compose stack (Postgres + MinIO).

Tests here are skipped automatically when the stack is not running. Start it with:
    docker compose -f deploy/docker-compose.yml --env-file .env up -d
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.api.routes import create_app
from gateway.core import crypto
from gateway.db.models import Base
from gateway.providers.base import ProviderError
from gateway.providers.registry import ProviderConfig, ProviderRegistry
from gateway.settings import Settings

REPO = Path(__file__).resolve().parents[2]
load_dotenv(REPO / ".env", override=False)

COMPOSE = ["docker", "compose", "-f", str(REPO / "deploy/docker-compose.yml"), "--env-file", str(REPO / ".env")]
PORTS = {"minio-in-1": 9010, "minio-in-2": 9020, "minio-abroad": 9030, "minio-extra": 9040}
JURISDICTION = {"minio-in-1": "IN", "minio-in-2": "IN", "minio-abroad": "NON_IN", "minio-extra": "NON_IN"}
FAST_KDF = crypto.KdfParams(time_cost=1, memory_cost_kib=8, parallelism=1)
TEST_DB = "sovereignstripe_test"


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def stack_up() -> bool:
    return bool(os.environ.get("DATABASE_URL")) and all(
        _port_open(p) for p in (5433, PORTS["minio-in-1"], PORTS["minio-in-2"], PORTS["minio-abroad"])
    )



def compose(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], check=True, capture_output=True, timeout=180)


async def wait_ready(provider_name: str, timeout_s: float = 60) -> None:
    """Wait until a (re)started MinIO answers S3 requests."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        p = ProviderRegistry.from_configs([provider_config(provider_name, "readiness-probe")])[provider_name]
        try:
            await p.open()
            await p.probe()
            return
        except ProviderError:
            await asyncio.sleep(1)
        finally:
            await p.close()
    raise TimeoutError(f"{provider_name} not ready after {timeout_s}s")


def provider_config(name: str, bucket: str) -> ProviderConfig:
    return ProviderConfig(
        name=name, type="s3", endpoint=f"http://127.0.0.1:{PORTS[name]}", bucket=bucket,
        jurisdiction=JURISDICTION[name], access_key_env="MINIO_ROOT_USER", secret_key_env="MINIO_ROOT_PASSWORD",
    )


@pytest.fixture(scope="session")
def test_db_url() -> str:
    """A dedicated database on the compose Postgres, created once."""
    url = make_url(os.environ["DATABASE_URL"])

    async def create() -> None:
        engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            exists = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB})
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
        await engine.dispose()

    asyncio.run(create())
    return url.set(database=TEST_DB).render_as_string(hide_password=False)


@pytest.fixture
async def fresh_db(test_db_url: str) -> str:
    engine = create_async_engine(test_db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    return test_db_url


@pytest.fixture
def bucket() -> str:
    return f"ss-test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def gateway(fresh_db: str, bucket: str):
    """Factory: gateway(provider_names) -> (http client, registry), cleaned up afterwards."""
    stack: list[tuple[object, ProviderRegistry]] = []

    async def start(names: list[str]) -> tuple[httpx.AsyncClient, ProviderRegistry]:
        registry = ProviderRegistry.from_configs([provider_config(n, bucket) for n in names])
        app = create_app(Settings(fresh_db, Path("unused"), "integration-pass", FAST_KDF), registry)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw", timeout=120)
        stack.append((lifespan, registry))
        stack.append((client, registry))
        return client, registry

    yield start

    for obj, registry in reversed(stack):
        if isinstance(obj, httpx.AsyncClient):
            await obj.aclose()
            await _empty_buckets(registry)
        else:
            await obj.__aexit__(None, None, None)  # type: ignore[attr-defined]


async def _empty_buckets(registry: ProviderRegistry) -> None:
    """Best effort: remove the per-test bucket from every reachable provider."""
    for p in registry.values():
        client = getattr(p, "_client", None)
        if client is None:
            continue
        try:
            listing = await client.list_objects_v2(Bucket=p.bucket)  # type: ignore[attr-defined]
            for obj in listing.get("Contents", []):
                await client.delete_object(Bucket=p.bucket, Key=obj["Key"])  # type: ignore[attr-defined]
            await client.delete_bucket(Bucket=p.bucket)  # type: ignore[attr-defined]
        except Exception:
            pass
