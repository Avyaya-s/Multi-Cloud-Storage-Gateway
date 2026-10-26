# SovereignStripe: Multi-Cloud Striped Storage Gateway

Cloud Computing course project (RVCE, IS355TBD). A gateway that encrypts files,
erasure-codes them with Reed–Solomon RS(4,2), and spreads the shards across
several storage providers so that any one provider can fail without data loss,
and no provider ever holds readable data.

## Tech stack
- Python 3.11+, FastAPI, asyncio
- Crypto: `cryptography` (AES-256-GCM), `argon2-cffi` (Argon2id)
- Erasure coding: `zfec` (use `zfec.easyfec`)
- Storage: `aioboto3` for S3-compatible providers (MinIO, Cloudflare R2); Google Drive API later
- Metadata: PostgreSQL + SQLAlchemy (async); SQLite is fine for early tests
- Workers: APScheduler
- Tests: pytest, pytest-asyncio, Hypothesis
- Deployment: Docker Compose (gateway, postgres, 3x MinIO, toxiproxy)

## Repository layout
```
gateway/
  service.py    FileService: upload/download/delete orchestration (pipeline + placer + repo)
  settings.py   env/.env settings
  api/          routes.py, auth.py
  core/         chunker.py, crypto.py, erasure.py, pipeline.py
  placement/    policy.py, placer.py, scorer.py
  providers/    base.py, s3_compat.py, gdrive.py, registry.py
  workers/      health.py, scrubber.py, rebalancer.py, gc.py
  db/           models.py, repo.py
  config/       providers.yaml, policies.yaml
dashboard/
deploy/         docker-compose.yml, toxiproxy.json
tests/          unit/, integration/, chaos/
bench/
```

## Core design (do not change without discussion)
- Files are split into fixed 4 MB segments; never load a whole file into memory.
- Envelope encryption:
  - Random 256-bit DEK per file; random 4-byte nonce prefix per file.
  - Each segment: AES-256-GCM, nonce = prefix (4 B) + seg_index (8 B big-endian).
  - AAD = file_id bytes + seg_index (8 B) + is_last (1 B) — prevents reordering and truncation.
  - DEK wrapped with a KEK (AES-GCM, random nonce, AAD b"dek-wrap"); KEK derived from an
    admin passphrase via Argon2id. Only the wrapped DEK is stored. KEK never persisted.
  - files.content_hmac uses an HMAC-SHA256 key derived from the KEK with HKDF-SHA256
    (`cryptography` library), info=b"content-hmac". Never use the KEK directly for anything
    other than DEK wrapping; every other key is derived via HKDF with its own info label.
- Encrypt FIRST, then erasure-code the ciphertext. Never the other way round.
- RS(4,2): k=4 data shards, m=2 parity shards; any 4 of 6 rebuild a segment.
- Each shard: SHA-256 fingerprint stored; object key = random UUID (no file names in the cloud).
- Placement rule: at most m shards of any segment on one provider.
- Metadata is write-ahead: rows inserted as PENDING before upload; file becomes ACTIVE
  (6/6 shards), DEGRADED (>= k+1, repair queued), or upload rolls back.
- Upload re-routing: a failed shard upload (after retries) is re-routed to any provider
  still under the m-shards-per-segment cap. With 3 providers there is no valid target, so
  re-routing is a no-op there; it works automatically once a 4th provider is configured.
  After re-routing: every segment >= k+1 shards stored -> DEGRADED + repair queued;
  any segment < k+1 -> roll back (delete uploaded shards) and return an error.
- On download: fetch k shards in parallel, verify SHA-256, decode, then decrypt (tag check).
  A failed fetch, failed hash or InvalidTag = treat shard as missing, fall back to the
  remaining shards, log CORRUPT for hash/tag failures.
- Hedged reads are NOT core: optional setting HEDGED_READS (default false), added later
  for benchmarking.
- Extension (only after core works): residency policies (india_only,
  recoverable_in_india, any), provider health scoring (EWMA + circuit breaker),
  rebalancer with hysteresis.

## Rules
- Never write custom crypto; use the `cryptography` library primitives only.
- Never log, print or return keys (DEK, KEK, passphrase).
- Secrets come from environment variables / `.env` (never committed).
- Every core module gets unit tests; crypto and erasure code get property-based tests.
- Keep functions small and typed (type hints everywhere).

## Commands
- Virtualenv: `.venv` (Python 3.13); `.venv/Scripts/python -m pip install -r requirements.txt`
- Run tests: `pytest -q` (integration tests auto-skip when the compose stack is down)
- Start stack: `docker compose -f deploy/docker-compose.yml --env-file .env up -d`
- Run API: `uvicorn gateway.api.routes:app --reload` (http://127.0.0.1:8000/docs)

## Local environment notes
- Ports: Postgres 127.0.0.1:5433 (5432 is taken by a local Postgres service);
  MinIO API/console: in-1 9010/9011, in-2 9020/9021, abroad 9030/9031, extra 9040/9041.
- Always use `127.0.0.1`, never `localhost`, in endpoints/URLs: on this Windows machine
  `localhost` tries IPv6 first and costs 5-20 s per new connection.
- Official minio/minio images are no longer pullable; compose uses Chainguard's MinIO
  build pinned by digest. `minio-extra` (compose profile "extra") is only for tests.
- KEK salt, Argon2id params and a verifier live in the `kek_meta` table; a wrong
  SS_ADMIN_PASSPHRASE is refused at startup.


## Background documents
- docs/synopsis.tex: submitted synopsis (objectives, methodology)
- docs/dpr-technical.md: detailed design, workflows, data model, APIs, test plan
- docs/dpr-problem-solution.md: problems, existing work, proposed solution
If these documents and CLAUDE.md disagree, CLAUDE.md wins.