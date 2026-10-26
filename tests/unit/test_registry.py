from pathlib import Path

import pytest

from gateway.providers.registry import ProviderRegistry, load_configs
from gateway.providers.s3_compat import S3CompatProvider

REPO = Path(__file__).resolve().parents[2]


def test_shipped_config_has_three_providers() -> None:
    configs = load_configs(REPO / "gateway/config/providers.yaml")
    assert [c.name for c in configs] == ["minio-in-1", "minio-in-2", "minio-abroad"]
    assert [c.jurisdiction for c in configs] == ["IN", "IN", "NON_IN"]


def test_build_reads_credentials_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIO_ROOT_USER", "u")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "s3cr3t-value")
    reg = ProviderRegistry.from_yaml(REPO / "gateway/config/providers.yaml")
    assert list(reg) == ["minio-in-1", "minio-in-2", "minio-abroad"]
    assert all(isinstance(p, S3CompatProvider) for p in reg.values())
    assert "s3cr3t-value" not in repr(reg["minio-in-1"])


def test_missing_credentials_env_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE", raising=False)
    cfg = tmp_path / "p.yaml"
    cfg.write_text(
        "providers:\n  - {name: a, type: s3, endpoint: 'http://x', bucket: b, jurisdiction: IN,"
        " access_key_env: NOPE, secret_key_env: NOPE}\n"
    )
    with pytest.raises(ValueError, match="NOPE"):
        ProviderRegistry.from_yaml(cfg)


@pytest.mark.parametrize(
    "body",
    [
        "providers:\n  - {name: a, type: s3, endpoint: x, bucket: b, jurisdiction: EU}\n",
        "providers:\n  - {name: a, type: s3, endpoint: x, bucket: b, jurisdiction: IN}\n"
        "  - {name: a, type: s3, endpoint: y, bucket: b, jurisdiction: IN}\n",
    ],
)
def test_invalid_configs_rejected(tmp_path: Path, body: str) -> None:
    cfg = tmp_path / "p.yaml"
    cfg.write_text(body)
    with pytest.raises(ValueError):
        load_configs(cfg)
