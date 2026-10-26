"""S3-compatible adapter (MinIO, Cloudflare R2, ...) on aioboto3."""

from __future__ import annotations

import time
from contextlib import AsyncExitStack
from typing import Any

import aioboto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from gateway.providers.base import ProviderError, ShardNotFoundError, StorageProvider

_NOT_FOUND = {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}

# Fail fast: the placer retries and re-routes, the download path falls back to other
# shards, so botocore's own long timeouts and retries would only add latency.
_BOTO_CONFIG = Config(
    connect_timeout=2,
    read_timeout=15,
    retries={"max_attempts": 1, "mode": "standard"},
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required",
)


def _is_not_found(err: ClientError) -> bool:
    return str(err.response.get("Error", {}).get("Code")) in _NOT_FOUND


class S3CompatProvider(StorageProvider):
    def __init__(
        self,
        name: str,
        jurisdiction: str,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        create_bucket: bool = True,
    ) -> None:
        super().__init__(name, jurisdiction)
        self.endpoint = endpoint
        self.bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._create_bucket = create_bucket
        self._stack: AsyncExitStack | None = None
        self._client: Any = None

    async def open(self) -> None:
        if self._client is not None:
            return
        self._stack = AsyncExitStack()
        self._client = await self._stack.enter_async_context(
            aioboto3.Session().client(
                "s3",
                endpoint_url=self.endpoint,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name=self._region,
                config=_BOTO_CONFIG,
            )
        )
        if self._create_bucket:
            await self._ensure_bucket()

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack, self._client = None, None

    async def _ensure_bucket(self) -> None:
        try:
            await self._client.head_bucket(Bucket=self.bucket)
        except ClientError as err:
            if not _is_not_found(err):
                raise ProviderError(f"{self.name}: cannot access bucket") from err
            await self._client.create_bucket(Bucket=self.bucket)
        except BotoCoreError as err:
            raise ProviderError(f"{self.name}: unreachable") from err

    def _require_client(self) -> Any:
        if self._client is None:
            raise ProviderError(f"{self.name}: provider not open")
        return self._client

    async def put(self, key: str, data: bytes) -> None:
        try:
            await self._require_client().put_object(Bucket=self.bucket, Key=key, Body=data)
        except (ClientError, BotoCoreError) as err:
            raise ProviderError(f"{self.name}: put failed") from err

    async def get(self, key: str) -> bytes:
        try:
            resp = await self._require_client().get_object(Bucket=self.bucket, Key=key)
            async with resp["Body"] as body:
                return await body.read()
        except ClientError as err:
            if _is_not_found(err):
                raise ShardNotFoundError(f"{self.name}: {key} not found") from err
            raise ProviderError(f"{self.name}: get failed") from err
        except BotoCoreError as err:
            raise ProviderError(f"{self.name}: get failed") from err

    async def head(self, key: str) -> bool:
        try:
            await self._require_client().head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as err:
            if _is_not_found(err):
                return False
            raise ProviderError(f"{self.name}: head failed") from err
        except BotoCoreError as err:
            raise ProviderError(f"{self.name}: head failed") from err

    async def delete(self, key: str) -> None:
        try:
            await self._require_client().delete_object(Bucket=self.bucket, Key=key)
        except (ClientError, BotoCoreError) as err:
            raise ProviderError(f"{self.name}: delete failed") from err

    async def probe(self) -> float:
        start = time.perf_counter()
        try:
            await self._require_client().head_bucket(Bucket=self.bucket)
        except (ClientError, BotoCoreError) as err:
            raise ProviderError(f"{self.name}: probe failed") from err
        return (time.perf_counter() - start) * 1000
