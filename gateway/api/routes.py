"""HTTP API. Run with: uvicorn gateway.api.routes:app --reload

No authentication yet (JWT comes with api/auth.py): bind to localhost only.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from gateway.db.models import File
from gateway.db.repo import Repo, make_engine
from gateway.placement.placer import Placer
from gateway.providers.registry import ProviderRegistry
from gateway.service import (
    FileNotAvailableError,
    FileService,
    NotEnoughShardsError,
    UploadFailedError,
    unlock_kek,
)
from gateway.settings import Settings

log = logging.getLogger(__name__)


class FileOut(BaseModel):
    id: uuid.UUID
    name: str
    size: int
    policy: str
    status: str
    segment_count: int | None
    created_at: datetime

    @classmethod
    def of(cls, f: File) -> FileOut:
        return cls(
            id=f.id, name=f.name, size=f.size, policy=f.policy, status=f.status.value,
            segment_count=f.segment_count, created_at=f.created_at,
        )


class ShardOut(BaseModel):
    shard_index: int
    provider: str | None
    status: str
    object_key: str
    size: int


class SegmentOut(BaseModel):
    seg_index: int
    plain_len: int
    shards: list[ShardOut]


class PlacementOut(BaseModel):
    file_id: uuid.UUID
    segments: list[SegmentOut]


def create_app(settings: Settings | None = None, registry: ProviderRegistry | None = None) -> FastAPI:
    """Build the app; tests inject settings and/or a provider registry."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or Settings.from_env()
        reg = registry or ProviderRegistry.from_yaml(cfg.providers_config)
        engine = make_engine(cfg.database_url)
        repo = Repo(engine)
        await repo.create_schema()
        await repo.sync_providers(reg.configs)
        kek = await unlock_kek(repo, cfg.admin_passphrase, cfg.kdf)
        await reg.open_all()
        app.state.service = FileService(repo, reg, Placer(reg), kek)
        try:
            yield
        finally:
            await reg.close_all()
            await engine.dispose()

    app = FastAPI(title="SovereignStripe", version="0.1.0", lifespan=lifespan)

    def svc() -> FileService:
        return app.state.service

    @app.post("/files", status_code=status.HTTP_201_CREATED, response_model=FileOut)
    async def upload(file: UploadFile, policy: str = Form("standard")) -> FileOut:
        try:
            stored = await svc().upload(file.filename or "unnamed", file.file, policy)
        except UploadFailedError as err:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(err)) from err
        return FileOut.of(stored)

    @app.get("/files", response_model=list[FileOut])
    async def list_files() -> list[FileOut]:
        return [FileOut.of(f) for f in await svc().repo.list_files()]

    @app.get("/files/{file_id}", response_model=FileOut)
    async def get_file(file_id: uuid.UUID) -> FileOut:
        f = await svc().repo.get_file(file_id)
        if f is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
        return FileOut.of(f)

    @app.get("/files/{file_id}/download")
    async def download(file_id: uuid.UUID) -> StreamingResponse:
        try:
            f, body = await svc().open_download(file_id)
        except FileNotAvailableError as err:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found or not available") from err
        except NotEnoughShardsError as err:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(err)) from err
        headers = {
            "Content-Length": str(f.size),  # lets clients detect a mid-stream failure
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(f.name)}",
        }
        return StreamingResponse(body, media_type="application/octet-stream", headers=headers)

    @app.get("/files/{file_id}/placement", response_model=PlacementOut)
    async def placement(file_id: uuid.UUID) -> PlacementOut:
        if await svc().repo.get_file(file_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
        segments = await svc().repo.get_placement(file_id)
        return PlacementOut(
            file_id=file_id,
            segments=[
                SegmentOut(
                    seg_index=s.seg_index, plain_len=s.plain_len,
                    shards=[
                        ShardOut(shard_index=sh.shard_index, provider=sh.provider_id,
                                 status=sh.status.value, object_key=sh.object_key, size=sh.size)
                        for sh in s.shards
                    ],
                )
                for s in segments
            ],
        )

    @app.delete("/files/{file_id}", status_code=status.HTTP_202_ACCEPTED)
    async def delete_file(file_id: uuid.UUID, background: BackgroundTasks) -> dict[str, str]:
        try:
            await svc().soft_delete(file_id)
        except FileNotAvailableError as err:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found") from err
        background.add_task(svc().purge, file_id)
        return {"id": str(file_id), "status": "DELETED"}

    return app


app = create_app()
