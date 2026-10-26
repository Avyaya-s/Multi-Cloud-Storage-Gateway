"""Load provider definitions from YAML and hold the live adapters."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gateway.providers.base import ProviderError, StorageProvider
from gateway.providers.s3_compat import S3CompatProvider

log = logging.getLogger(__name__)

JURISDICTIONS = {"IN", "NON_IN"}


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    type: str
    endpoint: str
    bucket: str
    jurisdiction: str
    cost_per_gb: float = 0.0
    region: str = "us-east-1"
    # Names of the environment variables holding credentials, never the secrets themselves.
    access_key_env: str = ""
    secret_key_env: str = ""
    enabled: bool = True

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ProviderConfig:
        cfg = cls(**raw)
        if cfg.jurisdiction not in JURISDICTIONS:
            raise ValueError(f"{cfg.name}: jurisdiction must be one of {sorted(JURISDICTIONS)}")
        return cfg


def load_configs(path: str | Path) -> list[ProviderConfig]:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    configs = [ProviderConfig.from_dict(p) for p in raw.get("providers", [])]
    names = [c.name for c in configs]
    if len(names) != len(set(names)):
        raise ValueError("provider names must be unique")
    return configs


def _env(var: str, provider: str) -> str:
    value = os.environ.get(var)
    if not value:
        raise ValueError(f"{provider}: environment variable {var} is not set")
    return value


def build_provider(cfg: ProviderConfig) -> StorageProvider:
    if cfg.type == "s3":
        return S3CompatProvider(
            name=cfg.name,
            jurisdiction=cfg.jurisdiction,
            endpoint=cfg.endpoint,
            bucket=cfg.bucket,
            access_key=_env(cfg.access_key_env, cfg.name),
            secret_key=_env(cfg.secret_key_env, cfg.name),
            region=cfg.region,
        )
    raise ValueError(f"{cfg.name}: unknown provider type {cfg.type!r}")


class ProviderRegistry(Mapping[str, StorageProvider]):
    """Name -> adapter, in configuration order (placement relies on a stable order)."""

    def __init__(self, providers: list[StorageProvider], configs: list[ProviderConfig] | None = None) -> None:
        self._providers = {p.name: p for p in providers}
        self.configs = configs or []

    @classmethod
    def from_configs(cls, configs: list[ProviderConfig]) -> ProviderRegistry:
        enabled = [c for c in configs if c.enabled]
        return cls([build_provider(c) for c in enabled], enabled)

    @classmethod
    def from_yaml(cls, path: str | Path) -> ProviderRegistry:
        return cls.from_configs(load_configs(path))

    def __getitem__(self, name: str) -> StorageProvider:
        return self._providers[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._providers)

    def __len__(self) -> int:
        return len(self._providers)

    async def open_all(self) -> None:
        """Open every provider; one being down at startup is logged, not fatal."""
        for provider in self._providers.values():
            try:
                await provider.open()
            except ProviderError as err:
                log.warning("provider %s unavailable at startup: %s", provider.name, err)

    async def close_all(self) -> None:
        for provider in self._providers.values():
            await provider.close()
