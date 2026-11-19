import io
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update

from gateway.api.routes import create_app
from gateway.db.models import File, FileStatus
from gateway.providers.registry import ProviderRegistry
from gateway.service import FileService, UploadFailedError
from gateway.settings import Settings
from gateway.workers.gc import GarbageCollector
from tests.fakes import memory_registry
from tests.unit.test_service import FAST_KDF, SEG, make_service, read_all, total_objects


async def setup(tmp_path: Path, *names: str, **gc_kw) -> tuple[ProviderRegistry, FileService, GarbageCollector]:
    reg = memory_registry(*(names or ("in-1", "in-2", "abroad")))
    svc = await make_service(tmp_path, reg)
    return reg, svc, GarbageCollector(svc.repo, reg, svc.placer, **gc_kw)


async def test_never_touches_live_files(tmp_path: Path) -> None:
    reg, svc, gc = await setup(tmp_path, sweep_min_age_s=0)
    data = os.urandom(SEG * 2)
    f = await svc.upload("x", io.BytesIO(data))
    report = await gc.run(sweep=True)
    assert report.objects_deleted == report.swept == report.orphans_deleted == 0
    assert total_objects(reg) == 12 and await read_all(svc, f.id) == data


async def test_orphans_are_deleted_and_forgotten(tmp_path: Path) -> None:
    reg, svc, gc = await setup(tmp_path)
    await reg["in-1"].put("old-copy", b"stale shard")
    await svc.repo.add_orphans([("in-1", "old-copy")], "repair-moved")
    report = await gc.run()
    assert report.orphans_deleted == 1 and "old-copy" not in reg["in-1"].blobs  # type: ignore[attr-defined]
    assert await svc.repo.orphans() == []


async def test_orphan_on_unreachable_provider_is_kept_for_later(tmp_path: Path) -> None:
    reg, svc, gc = await setup(tmp_path)
    await reg["abroad"].put("old-copy", b"stale shard")
    await svc.repo.add_orphans([("abroad", "old-copy")], "repair-moved")
    reg["abroad"].down = True  # type: ignore[attr-defined]
    assert (await gc.run()).orphans_kept == 1
    assert len(await svc.repo.orphans()) == 1  # still on record
    reg["abroad"].down = False  # type: ignore[attr-defined]
    assert (await gc.run()).orphans_deleted == 1
    assert "old-copy" not in reg["abroad"].blobs  # type: ignore[attr-defined]


async def test_leftovers_of_a_failed_upload_are_removed(tmp_path: Path) -> None:
    reg, svc, gc = await setup(tmp_path)
    real_put = reg["in-2"].put

    async def put_then_die(key: str, data: bytes) -> None:  # stores the object, then the cloud vanishes
        await real_put(key, data)
        reg["in-2"].down = True  # type: ignore[attr-defined]

    reg["in-2"].put = put_then_die  # type: ignore[method-assign]
    with pytest.raises(UploadFailedError):
        await svc.upload("x", io.BytesIO(os.urandom(SEG * 3)))
    assert total_objects(reg) > 0  # the rollback could not delete everything

    reg["in-2"].down = False  # type: ignore[attr-defined]
    report = await gc.run()
    assert report.objects_deleted > 0 and report.objects_kept == 0 and report.files_removed == 1
    assert total_objects(reg) == 0
    assert await svc.repo.list_files(include_deleted=True) == []


async def test_delete_that_hit_a_down_cloud_is_finished(tmp_path: Path) -> None:
    reg, svc, gc = await setup(tmp_path)
    f = await svc.upload("x", io.BytesIO(os.urandom(SEG * 2)))
    await svc.soft_delete(f.id)
    reg["abroad"].down = True  # type: ignore[attr-defined]
    await svc.purge(f.id)  # the background purge after DELETE
    assert len(reg["abroad"].blobs) == 4  # type: ignore[attr-defined]

    assert (await gc.run()).objects_kept == 4  # still down: nothing lost, retried later
    reg["abroad"].down = False  # type: ignore[attr-defined]
    report = await gc.run()
    assert report.objects_deleted == 4 and report.files_removed == 1
    assert total_objects(reg) == 0 and await svc.repo.get_file(f.id) is None


async def test_stale_pending_upload_is_given_up_and_cleaned(tmp_path: Path) -> None:
    reg, svc, gc = await setup(tmp_path, pending_grace_s=3600)
    stale, fresh = os.urandom(16), os.urandom(16)
    from uuid import UUID
    for raw in (stale, fresh):
        await svc.repo.create_file(UUID(bytes=raw), "x", "standard", b"w", b"nonc")
    async with svc.repo.session() as s, s.begin():
        await s.execute(update(File).where(File.id == UUID(bytes=stale))
                        .values(created_at=datetime.now(UTC) - timedelta(hours=2)))

    report = await gc.run()
    assert report.stale_uploads == 1 and report.files_removed == 1
    assert await svc.repo.get_file(UUID(bytes=stale)) is None
    assert (await svc.repo.get_file(UUID(bytes=fresh))).status is FileStatus.PENDING  # type: ignore[union-attr]


async def test_abandoned_upload_is_not_resurrected(tmp_path: Path) -> None:
    reg, svc, gc = await setup(tmp_path)
    real_put = reg["in-1"].put

    async def gc_gives_up_midway(key: str, data: bytes) -> None:
        for f in await svc.repo.files_with_status(FileStatus.PENDING):
            await svc.repo.set_file_status(f.id, FileStatus.FAILED)  # what a stale-upload GC does
        await real_put(key, data)

    reg["in-1"].put = gc_gives_up_midway  # type: ignore[method-assign]
    with pytest.raises(UploadFailedError, match="abandoned"):
        await svc.upload("x", io.BytesIO(os.urandom(SEG)))
    assert await svc.repo.list_files() == [] and total_objects(reg) == 0


async def test_unplaceable_shard_rows_do_not_keep_a_dead_file_alive(tmp_path: Path) -> None:
    reg, svc, gc = await setup(tmp_path, "a", "b")  # 2 providers x cap 2 = only 4 of 6 shards placeable
    with pytest.raises(UploadFailedError):
        await svc.upload("x", io.BytesIO(os.urandom(SEG)))
    assert await svc.repo.list_files(include_deleted=True) == []  # fully cleaned by the rollback
    assert total_objects(reg) == 0


async def test_sweep_deletes_only_old_untracked_objects(tmp_path: Path) -> None:
    reg, svc, gc = await setup(tmp_path, sweep_min_age_s=3600)
    f = await svc.upload("x", io.BytesIO(os.urandom(SEG)))
    in1 = reg["in-1"]
    await in1.put("ancient-stray", b"?")
    await in1.put("brand-new-stray", b"?")  # might belong to an upload in flight: too young to judge
    in1.mtimes["ancient-stray"] = datetime.now(UTC) - timedelta(days=2)  # type: ignore[attr-defined]
    reg["abroad"].down = True  # type: ignore[attr-defined]

    report = await gc.run(sweep=True)
    assert report.swept == 1 and report.sweep_skipped == ["abroad"]
    assert "ancient-stray" not in in1.blobs and "brand-new-stray" in in1.blobs  # type: ignore[attr-defined]
    reg["abroad"].down = False  # type: ignore[attr-defined]
    assert len((await read_all(svc, f.id))) == SEG  # tracked shards untouched


async def test_admin_gc_endpoint(tmp_path: Path) -> None:
    settings = Settings(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}", Path("unused"), "pw", FAST_KDF)
    reg = memory_registry("in-1", "in-2", "abroad")
    app = create_app(settings, reg)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await app.state.service.repo.add_orphans([("in-2", "gone-already")], "repair-moved")
            r = await c.post("/admin/gc")
            assert r.status_code == 200, r.text
            assert r.json()["orphans_deleted"] == 1 and r.json()["sweep"] is False
            assert (await c.get("/events", params={"limit": 1})).json()[0]["type"] == "GC"
