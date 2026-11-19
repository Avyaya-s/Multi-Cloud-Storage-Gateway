import asyncio
import io
import os
from pathlib import Path

import httpx
import pytest

from gateway.api.routes import create_app
from gateway.db.models import FileStatus, ShardStatus
from gateway.providers.registry import ProviderRegistry
from gateway.service import FileService
from gateway.settings import Settings
from gateway.workers.scrubber import Scrubber, ScrubBusyError
from tests.fakes import memory_registry
from tests.unit.test_service import FAST_KDF, SEG, make_service, read_all


async def setup(tmp_path: Path, *names: str) -> tuple[ProviderRegistry, FileService, Scrubber]:
    reg = memory_registry(*(names or ("in-1", "in-2", "abroad")))
    svc = await make_service(tmp_path, reg)
    return reg, svc, Scrubber(svc.repo, reg, svc.placer)


async def shard_rows(svc: FileService, file_id):
    return [sh for seg in await svc.repo.get_placement(file_id) for sh in seg.shards]


async def test_healthy_file_needs_nothing(tmp_path: Path) -> None:
    _, svc, scrubber = await setup(tmp_path)
    await svc.upload("x", io.BytesIO(os.urandom(SEG * 2)))
    report = await scrubber.run(deep=True)
    assert (report.files, report.shards_checked, report.repaired) == (1, 12, 0)
    assert report.missing_found == report.corrupt_found == report.unrepaired == 0


async def test_quick_scrub_rebuilds_a_deleted_shard_in_place(tmp_path: Path) -> None:
    reg, svc, scrubber = await setup(tmp_path)
    data = os.urandom(SEG * 2 + 5)
    f = await svc.upload("x", io.BytesIO(data))
    victim = (await shard_rows(svc, f.id))[3]
    original = reg[victim.provider_id].blobs.pop(victim.object_key)  # type: ignore[attr-defined]

    report = await scrubber.run(deep=False)

    assert (report.missing_found, report.repaired, report.unrepaired) == (1, 1, 0)
    # Rebuilt without any key, byte-identical, same provider and object key.
    assert reg[victim.provider_id].blobs[victim.object_key] == original  # type: ignore[attr-defined]
    assert all(sh.status is ShardStatus.STORED for sh in await shard_rows(svc, f.id))
    assert "REPAIR" in [e.type for e in await svc.repo.events()]
    assert await read_all(svc, f.id) == data


async def test_only_deep_scrub_finds_silent_corruption(tmp_path: Path) -> None:
    reg, svc, scrubber = await setup(tmp_path)
    data = os.urandom(SEG)
    f = await svc.upload("x", io.BytesIO(data))
    victim = (await shard_rows(svc, f.id))[0]
    blobs = reg[victim.provider_id].blobs  # type: ignore[attr-defined]
    good = blobs[victim.object_key]
    blobs[victim.object_key] = os.urandom(len(good))  # bit rot: object exists, content wrong

    quick = await scrubber.run(deep=False)
    assert quick.corrupt_found == 0 and quick.repaired == 0  # HEAD cannot see it

    deep = await scrubber.run(deep=True)
    assert (deep.corrupt_found, deep.repaired) == (1, 1)
    assert blobs[victim.object_key] == good


async def test_repairs_shards_marked_corrupt_by_a_download(tmp_path: Path) -> None:
    reg, svc, scrubber = await setup(tmp_path)
    data = os.urandom(SEG)
    f = await svc.upload("x", io.BytesIO(data))
    victim = (await shard_rows(svc, f.id))[0]
    reg[victim.provider_id].blobs[victim.object_key] = b"\x00" * victim.size  # type: ignore[attr-defined]
    assert await read_all(svc, f.id) == data  # download marks it CORRUPT
    assert (await shard_rows(svc, f.id))[0].status is ShardStatus.CORRUPT

    report = await scrubber.run(deep=False)
    assert report.repaired == 1
    assert (await shard_rows(svc, f.id))[0].status is ShardStatus.STORED


async def test_degraded_upload_is_healed(tmp_path: Path) -> None:
    reg, svc, scrubber = await setup(tmp_path)
    reg["abroad"].poison_first_key = True  # type: ignore[attr-defined]
    f = await svc.upload("x", io.BytesIO(os.urandom(SEG * 2)))
    assert f.status is FileStatus.DEGRADED
    reg["abroad"].poison_first_key = False  # type: ignore[attr-defined]  # the cloud recovers

    report = await scrubber.run()
    assert report.repaired == 1 and report.healed_files == [str(f.id)]
    assert (await svc.repo.get_file(f.id)).status is FileStatus.ACTIVE  # type: ignore[union-attr]
    assert "FILE_HEALED" in [e.type for e in await svc.repo.events()]


async def test_unreachable_provider_is_not_treated_as_data_loss(tmp_path: Path) -> None:
    reg, svc, scrubber = await setup(tmp_path)
    f = await svc.upload("x", io.BytesIO(os.urandom(SEG * 2)))
    reg["abroad"].down = True  # type: ignore[attr-defined]

    report = await scrubber.run(deep=True)
    assert report.unreachable == 4 and report.missing_found == 0 and report.repaired == 0
    assert all(sh.status is ShardStatus.STORED for sh in await shard_rows(svc, f.id))
    assert (await svc.repo.get_file(f.id)).status is FileStatus.ACTIVE  # type: ignore[union-attr]


async def test_too_few_shards_left_marks_file_degraded(tmp_path: Path) -> None:
    reg, svc, scrubber = await setup(tmp_path)
    f = await svc.upload("x", io.BytesIO(os.urandom(SEG)))
    for sh in (await shard_rows(svc, f.id))[:3]:  # 3 of 6 gone: only 3 left, need 4
        reg[sh.provider_id].blobs.pop(sh.object_key)  # type: ignore[attr-defined]

    report = await scrubber.run()
    assert report.missing_found == 3 and report.repaired == 0 and report.unrepaired == 3
    assert (await svc.repo.get_file(f.id)).status is FileStatus.DEGRADED  # type: ignore[union-attr]
    assert "REPAIR_FAILED" in [e.type for e in await svc.repo.events()]


async def test_moves_shard_to_another_provider_and_records_orphan(tmp_path: Path) -> None:
    reg, svc, scrubber = await setup(tmp_path, "in-1", "in-2", "abroad", "extra")
    data = os.urandom(SEG)
    f = await svc.upload("x", io.BytesIO(data))
    victim = next(sh for sh in await shard_rows(svc, f.id) if sh.provider_id == "abroad")
    await svc.repo.set_shard_statuses({victim.id: ShardStatus.CORRUPT})
    reg["abroad"].down = True  # type: ignore[attr-defined]  # cannot rewrite it in place

    report = await scrubber.run()
    assert report.repaired == 1
    moved = next(sh for sh in await shard_rows(svc, f.id) if sh.id == victim.id)
    assert moved.provider_id != "abroad" and moved.object_key != victim.object_key
    assert moved.status is ShardStatus.STORED
    # The old copy may still exist on 'abroad': it is on record for the garbage collector.
    assert [(o.provider_id, o.object_key) for o in await svc.repo.orphans()] == [("abroad", victim.object_key)]
    reg["abroad"].down = False  # type: ignore[attr-defined]
    assert await read_all(svc, f.id) == data


async def test_no_room_to_move_leaves_shard_missing(tmp_path: Path) -> None:
    reg, svc, scrubber = await setup(tmp_path)  # 3 providers, each already holds 2 per segment
    f = await svc.upload("x", io.BytesIO(os.urandom(SEG)))
    victim = next(sh for sh in await shard_rows(svc, f.id) if sh.provider_id == "abroad")
    reg["abroad"].blobs.pop(victim.object_key)  # type: ignore[attr-defined]
    real_put = reg["abroad"].put

    async def refuse(key: str, data: bytes) -> None:
        raise RuntimeError("quota exceeded")

    reg["abroad"].put = refuse  # type: ignore[method-assign]
    report = await scrubber.run()
    assert report.repaired == 0 and report.unrepaired == 1
    assert next(sh for sh in await shard_rows(svc, f.id) if sh.id == victim.id).status is ShardStatus.MISSING
    reg["abroad"].put = real_put  # type: ignore[method-assign]
    assert (await scrubber.run()).repaired == 1  # fixed on the next run once the cloud accepts writes


async def test_concurrent_runs_are_refused(tmp_path: Path) -> None:
    _, svc, scrubber = await setup(tmp_path)
    await svc.upload("x", io.BytesIO(os.urandom(SEG)))
    first = asyncio.create_task(scrubber.run(deep=True))
    await asyncio.sleep(0)
    with pytest.raises(ScrubBusyError):
        await scrubber.run()
    await first


async def test_admin_scrub_endpoint(tmp_path: Path) -> None:
    settings = Settings(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}", Path("unused"), "pw", FAST_KDF)
    reg = memory_registry("in-1", "in-2", "abroad")
    app = create_app(settings, reg)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            fid = (await c.post("/files", files={"file": ("a.bin", os.urandom(1000))})).json()["id"]
            sh = (await c.get(f"/files/{fid}/placement")).json()["segments"][0]["shards"][1]
            reg[sh["provider"]].blobs.pop(sh["object_key"])  # type: ignore[attr-defined]

            r = await c.post("/admin/scrub", params={"deep": "true"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["deep"] is True and body["missing_found"] == 1 and body["repaired"] == 1
            assert (await c.get("/events", params={"limit": 1})).json()[0]["type"] == "SCRUB"
