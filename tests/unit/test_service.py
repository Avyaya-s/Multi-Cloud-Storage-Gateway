import io
import os
from collections import Counter
from pathlib import Path

import pytest

from gateway.core import crypto
from gateway.db.models import FileStatus, ShardStatus
from gateway.db.repo import Repo, make_engine
from gateway.placement.placer import Placer
from gateway.providers.registry import ProviderRegistry
from gateway.service import FileNotAvailableError, FileService, UploadFailedError, unlock_kek
from tests.fakes import memory_registry

FAST_KDF = crypto.KdfParams(time_cost=1, memory_cost_kib=8, parallelism=1)
SEG = 1024


async def make_service(tmp_path: Path, reg: ProviderRegistry, passphrase: str = "pw") -> FileService:
    repo = Repo(make_engine(f"sqlite+aiosqlite:///{tmp_path / 'meta.db'}"))
    await repo.create_schema()
    await repo.sync_providers(reg.configs)
    kek = await unlock_kek(repo, passphrase, FAST_KDF)
    return FileService(repo, reg, Placer(reg, backoff_s=0), kek, segment_size=SEG)


async def read_all(svc: FileService, file_id) -> bytes:
    _, body = await svc.open_download(file_id)
    return b"".join([c async for c in body])


def total_objects(reg: ProviderRegistry) -> int:
    return sum(len(p.blobs) for p in reg.values())  # type: ignore[attr-defined]


async def test_round_trip_active(tmp_path: Path) -> None:
    reg = memory_registry("in-1", "in-2", "abroad")
    svc = await make_service(tmp_path, reg)
    data = os.urandom(SEG * 3 + 7)
    f = await svc.upload("results.pdf", io.BytesIO(data))
    assert f.status is FileStatus.ACTIVE and f.size == len(data) and f.segment_count == 4
    assert f.content_hmac and len(f.content_hmac) == 64
    assert await read_all(svc, f.id) == data
    for seg in await svc.repo.get_placement(f.id):
        assert [sh.status for sh in seg.shards] == [ShardStatus.STORED] * 6
        assert max(Counter(sh.provider_id for sh in seg.shards).values()) == 2
    assert total_objects(reg) == 4 * 6


async def test_empty_file(tmp_path: Path) -> None:
    svc = await make_service(tmp_path, memory_registry("a", "b", "c"))
    f = await svc.upload("empty", io.BytesIO(b""))
    assert f.segment_count == 1 and await read_all(svc, f.id) == b""


async def test_download_survives_one_provider_down(tmp_path: Path) -> None:
    reg = memory_registry("in-1", "in-2", "abroad")
    svc = await make_service(tmp_path, reg)
    data = os.urandom(SEG * 5)
    f = await svc.upload("x", io.BytesIO(data))
    reg["abroad"].down = True  # type: ignore[attr-defined]
    reg["abroad"].get_calls = 0  # type: ignore[attr-defined]
    assert await read_all(svc, f.id) == data
    # Once it fails, later segments try the dead provider's shards last (i.e. not at all).
    assert reg["abroad"].get_calls <= 2  # type: ignore[attr-defined]


async def test_provider_down_during_upload_rolls_back(tmp_path: Path) -> None:
    reg = memory_registry("in-1", "in-2", "abroad")
    reg["in-2"].down = True  # type: ignore[attr-defined]
    svc = await make_service(tmp_path, reg)
    with pytest.raises(UploadFailedError, match="only 4/6"):
        await svc.upload("x", io.BytesIO(os.urandom(SEG * 2)))
    assert total_objects(reg) == 0
    # Rows naming the unreachable provider stay (file FAILED) for the garbage collector:
    # a put that errored may still have left an object behind.
    (f,) = await svc.repo.list_files(include_deleted=True)
    assert f.status is FileStatus.FAILED
    assert {p for p, _ in await svc.repo.all_shard_locations(f.id)} == {"in-2"}
    assert [e.type for e in await svc.repo.events()] == ["ROLLBACK"]


async def test_rollback_with_reachable_providers_leaves_nothing(tmp_path: Path) -> None:
    reg = memory_registry("in-1", "in-2", "abroad")
    svc = await make_service(tmp_path, reg)
    calls = 0
    real_put = reg["abroad"].put

    async def fail_after_first_segment(key: str, data: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls > 2:  # segment 0 fine, then both abroad shards of segment 1 fail
            raise RuntimeError("quota exceeded")
        await real_put(key, data)

    reg["abroad"].put = fail_after_first_segment  # type: ignore[method-assign]
    with pytest.raises(UploadFailedError):
        await svc.upload("x", io.BytesIO(os.urandom(SEG * 2)))
    assert total_objects(reg) == 0
    assert await svc.repo.list_files(include_deleted=True) == []


async def test_rollback_keeps_rows_for_objects_it_could_not_delete(tmp_path: Path) -> None:
    reg = memory_registry("in-1", "in-2", "abroad")
    svc = await make_service(tmp_path, reg)
    real_put = reg["in-2"].put

    async def put_then_die(key: str, data: bytes) -> None:
        await real_put(key, data)
        reg["in-2"].down = True  # type: ignore[attr-defined]

    reg["in-2"].put = put_then_die  # type: ignore[method-assign]
    with pytest.raises(UploadFailedError):
        await svc.upload("x", io.BytesIO(os.urandom(SEG * 3)))
    (f,) = await svc.repo.list_files(include_deleted=True)
    assert f.status is FileStatus.FAILED
    leftover = await svc.repo.all_shard_locations(f.id)
    assert leftover and {p for p, _ in leftover} == {"in-2"}


async def test_single_shard_failure_is_degraded_and_repair_queued(tmp_path: Path) -> None:
    reg = memory_registry("in-1", "in-2", "abroad")
    svc = await make_service(tmp_path, reg)
    reg["abroad"].poison_first_key = True  # type: ignore[attr-defined]
    data = os.urandom(SEG * 2)
    f = await svc.upload("x", io.BytesIO(data))
    assert f.status is FileStatus.DEGRADED
    statuses = [sh.status for seg in await svc.repo.get_placement(f.id) for sh in seg.shards]
    assert statuses.count(ShardStatus.MISSING) == 1
    assert "REPAIR_QUEUED" in [e.type for e in await svc.repo.events()]
    assert await read_all(svc, f.id) == data


async def test_four_providers_reroute_keeps_file_active(tmp_path: Path) -> None:
    reg = memory_registry("in-1", "in-2", "abroad", "extra")
    reg["extra"].down = True  # type: ignore[attr-defined]
    svc = await make_service(tmp_path, reg)
    data = os.urandom(SEG * 3)
    f = await svc.upload("x", io.BytesIO(data))
    assert f.status is FileStatus.ACTIVE
    reroutes = await svc.repo.events("REROUTE")
    assert reroutes and all(e.details_json["from"] == "extra" for e in reroutes)
    for seg in await svc.repo.get_placement(f.id):
        providers = [sh.provider_id for sh in seg.shards]
        assert "extra" not in providers and max(Counter(providers).values()) <= 2
    assert await read_all(svc, f.id) == data


async def test_corrupt_shard_marked(tmp_path: Path) -> None:
    reg = memory_registry("in-1", "in-2", "abroad")
    svc = await make_service(tmp_path, reg)
    data = os.urandom(SEG)
    f = await svc.upload("x", io.BytesIO(data))
    (seg,) = await svc.repo.get_placement(f.id)
    sh0 = seg.shards[0]
    reg[sh0.provider_id].blobs[sh0.object_key] = b"\x00" * sh0.size  # type: ignore[attr-defined,index]
    assert await read_all(svc, f.id) == data
    (seg,) = await svc.repo.get_placement(f.id)
    assert seg.shards[0].status is ShardStatus.CORRUPT


async def test_soft_delete_and_purge(tmp_path: Path) -> None:
    reg = memory_registry("in-1", "in-2", "abroad")
    svc = await make_service(tmp_path, reg)
    f = await svc.upload("x", io.BytesIO(b"hello"))
    await svc.soft_delete(f.id)
    with pytest.raises(FileNotAvailableError):
        await svc.open_download(f.id)
    assert await svc.repo.list_files() == []
    await svc.purge(f.id)
    assert total_objects(reg) == 0 and await svc.repo.get_file(f.id) is None


async def test_wrong_passphrase_refused(tmp_path: Path) -> None:
    reg = memory_registry("a")
    await make_service(tmp_path, reg, passphrase="right")
    with pytest.raises(RuntimeError, match="passphrase"):
        await make_service(tmp_path, reg, passphrase="wrong")


async def test_same_passphrase_reopens_existing_files(tmp_path: Path) -> None:
    reg = memory_registry("a", "b", "c")
    svc = await make_service(tmp_path, reg)
    f = await svc.upload("x", io.BytesIO(b"persisted"))
    svc2 = await make_service(tmp_path, reg)
    assert await read_all(svc2, f.id) == b"persisted"
