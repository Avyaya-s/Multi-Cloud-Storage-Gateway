import os
from pathlib import Path

import httpx

from gateway.api.routes import create_app
from gateway.core import crypto
from gateway.settings import Settings
from tests.fakes import memory_registry

FAST_KDF = crypto.KdfParams(time_cost=1, memory_cost_kib=8, parallelism=1)


async def test_endpoints_end_to_end(tmp_path: Path) -> None:
    settings = Settings(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}", Path("unused"), "pw", FAST_KDF)
    app = create_app(settings, memory_registry("in-1", "in-2", "abroad"))
    data = os.urandom(5 * 1024 * 1024)  # 2 real 4 MiB segments
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/files", files={"file": ("résumé.pdf", data)}, data={"policy": "sensitive"})
            assert r.status_code == 201, r.text
            meta = r.json()
            assert meta["status"] == "ACTIVE" and meta["segment_count"] == 2 and meta["policy"] == "sensitive"
            fid = meta["id"]

            assert [f["id"] for f in (await c.get("/files")).json()] == [fid]
            assert (await c.get(f"/files/{fid}")).json()["size"] == len(data)

            r = await c.get(f"/files/{fid}/download")
            assert r.status_code == 200 and r.content == data
            assert "UTF-8''r%C3%A9sum%C3%A9.pdf" in r.headers["content-disposition"]

            placement = (await c.get(f"/files/{fid}/placement")).json()
            assert [len(s["shards"]) for s in placement["segments"]] == [6, 6]

            assert (await c.delete(f"/files/{fid}")).status_code == 202
            assert (await c.get(f"/files/{fid}/download")).status_code == 404
            assert (await c.get("/files")).json() == []
            assert (await c.get(f"/files/{os.urandom(16).hex()}")).status_code == 404


async def test_dashboard_endpoints(tmp_path: Path) -> None:
    settings = Settings(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}", Path("unused"), "pw", FAST_KDF)
    reg = memory_registry("in-1", "in-2", "abroad")
    app = create_app(settings, reg)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await c.post("/files", files={"file": ("a.txt", b"hello")})
            reg["abroad"].down = True  # type: ignore[attr-defined]

            providers = {p["name"]: p for p in (await c.get("/providers")).json()}
            assert list(providers) == ["in-1", "in-2", "abroad"]
            assert providers["in-1"]["up"] and providers["in-1"]["latency_ms"] is not None
            assert not providers["abroad"]["up"] and providers["abroad"]["latency_ms"] is None
            assert providers["abroad"]["jurisdiction"] == "NON_IN"
            assert all(p["shards"] == {"STORED": 2} for p in providers.values())

            # A deleted file whose shards could not all be purged is GC work, not live data.
            fid = (await c.get("/files")).json()[0]["id"]
            await c.delete(f"/files/{fid}")
            providers = {p["name"]: p for p in (await c.get("/providers")).json()}
            assert all(p["shards"] == {} for p in providers.values())

            events = (await c.get("/events", params={"limit": 5})).json()
            assert [e["type"] for e in events] == ["DELETE", "UPLOAD"]  # newest first
            assert events[1]["details"] == {"file_id": fid, "status": "ACTIVE"}
            assert (await c.get("/events", params={"limit": 0})).status_code == 422
