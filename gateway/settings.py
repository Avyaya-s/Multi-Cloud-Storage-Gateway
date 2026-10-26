"""Runtime settings from environment variables (and .env in development)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from gateway.core.crypto import KdfParams

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_url: str
    providers_config: Path
    admin_passphrase: str = field(repr=False)
    kdf: KdfParams = KdfParams()

    @classmethod
    def from_env(cls, env_file: Path | None = REPO_ROOT / ".env") -> Settings:
        if env_file is not None and env_file.exists():
            load_dotenv(env_file, override=False)
        passphrase = os.environ.get("SS_ADMIN_PASSPHRASE", "")
        if not passphrase:
            raise RuntimeError("SS_ADMIN_PASSPHRASE is not set (see .env.example)")
        return cls(
            database_url=os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./gateway.db"),
            providers_config=Path(
                os.environ.get("PROVIDERS_CONFIG", REPO_ROOT / "gateway/config/providers.yaml")
            ),
            admin_passphrase=passphrase,
        )
