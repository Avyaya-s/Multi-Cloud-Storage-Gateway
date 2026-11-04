# How SovereignStripe Works

This walk-through follows one real example, a **10 MB PDF called `marks.pdf`**, from the moment the gateway starts to the moment the file is deleted.

```
           ┌───────────────────────────┐
 Browser / │  api/routes.py  (FastAPI) │  HTTP in/out
 curl      └─────────────┬─────────────┘
                         ▼
           ┌───────────────────────────┐
           │  service.py (FileService) │  coordinates every operation
           └──┬──────────┬──────────┬──┘
              ▼          ▼          ▼
        core/        placement/    db/
        (encrypt,    placer.py     repo.py + models.py
         erasure-    (which cloud  (PostgreSQL: what is where)
         code)        gets what)
                         ▼
                    providers/  ──►  MinIO in-1 │ MinIO in-2 │ MinIO abroad
```

---

## Step 0: The gateway wakes up

When you run `uvicorn gateway.api.routes:app`, the gateway does three things before it accepts any files:

1. **Reads `.env`.** This is where the secrets live: the database address, the MinIO passwords and the **admin passphrase**.
2. **Turns the passphrase into a master key (the KEK).**
   - It feeds the passphrase into **Argon2id**, a deliberately slow algorithm (about 64 MB of memory and a fraction of a second). That slowness makes guessing passphrases very expensive for an attacker.
   - On the very first run it saves a random "salt" and a small "check value" in the database (`kek_meta` table).
   - On every run after that, it re-derives the key and tests it against the check value. A wrong passphrase stops the gateway from starting.
   - The **KEK lives only in RAM**. It is never written to disk.
3. **Connects to the clouds.** It reads `gateway/config/providers.yaml` and opens a connection to each of the three MinIO servers: `minio-in-1`, `minio-in-2` and `minio-abroad`.

The gateway is now ready.

---

## Step 1: You upload `marks.pdf`

You click **Execute** on `POST /files` in Swagger, or the demo script does it for you.

### 1a. A record is created before anything is stored
The gateway creates a row in the `files` table for `marks.pdf` with status **PENDING**.
- It generates a **fresh random key just for this file**, called the **DEK** (data encryption key).
- It locks the DEK with the KEK (it "wraps" it) and stores **only the locked version**. Anyone who reads the database sees a locked box with no key.

### 1b. The file is cut into 4 MB pieces (`core/chunker.py`)
A 10 MB file becomes **3 segments**: 4 MB, 4 MB and 2 MB. The gateway reads **one segment at a time**, so even a 5 GB file never sits fully in memory. The last segment is labelled "this is the last one".

While reading, it also computes a fingerprint (HMAC) of the original file. Later, this can prove the file you get back is exactly the one you uploaded.

### 1c. Each segment is encrypted (`core/crypto.py`)
Each 4 MB segment is locked with **AES-256-GCM** using the file's DEK. Two details matter here:

- **Every segment gets a unique nonce** (segment 0, segment 1, and so on). Encrypting two pieces in exactly the same way would weaken the encryption.
- **Each segment is stamped with an "address label"** (the AAD): *"I am segment 2 of file X, and I am the last one."* The label isn't secret, but it is sealed into the encryption. If someone swaps segments around or chops off the end of the file, decryption fails loudly.

After this step the segment is unreadable gibberish plus a 16-byte **tag**, a tamper-proof seal.

### 1d. Each encrypted segment is split into 6 shards (`core/erasure.py`)
This is the Reed–Solomon **RS(4,2)** step.

- The encrypted segment is cut into **4 data pieces** of about 1 MB each.
- 2 extra **parity pieces** are calculated from them.
- That gives **6 shards**, and **any 4 of the 6 are enough to rebuild the segment.**

Think of it like this: you need 4 puzzle pieces to finish the picture, but you made 6, so you can lose any 2 and still finish it. Plain duplication would need 3 full copies (3× the storage) to survive the same losses. RS(4,2) uses only **1.5×**.

Each shard also gets:
- a **SHA-256 fingerprint**, so corruption can be detected later
- a **random UUID name** like `7f3a9c…`, so the cloud never sees "marks.pdf"

**Order matters: encrypt first, then split.** Every shard is pieces of ciphertext, so no shard contains any readable data.

### 1e. The placer decides which cloud gets which shard (`placement/placer.py`)
The rule: **no cloud may hold more than 2 shards of the same segment.**

| Segment | minio-in-1 | minio-in-2 | minio-abroad |
|---|---|---|---|
| 0 | shards 0, 3 | shards 1, 4 | shards 2, 5 |
| 1 | (layout rotated) | … | … |

Why 2? If one whole cloud disappears, you lose at most 2 shards and still have 4. That is exactly enough to rebuild.

### 1f. Write-ahead: write down the plan, then act (`db/repo.py`)
Before uploading anything, the gateway writes **6 shard rows marked PENDING**: *"shard 0 → in-1, key 7f3a…"*, and so on.

Why first? If the gateway crashes halfway, the database still knows exactly which objects **might** exist in which cloud. Nothing is left lying around without a record, and cleanup stays possible.

### 1g. Upload the 6 shards in parallel (`providers/s3_compat.py`)
All 6 shards go out at once.
- If one upload fails, it's **retried 3 times**, waiting a bit longer each time (0.2 s, 0.4 s, …).
- If it **still** fails, the gateway tries to **re-route** the shard to another cloud that has fewer than 2 shards of this segment. With 3 clouds, every cloud is already full at 2, so there's nowhere to move it. With a 4th cloud there is, and integration test (c) proves this works. Each re-route is logged as a `REROUTE` event.

Each row then becomes **STORED** or **MISSING**.

### 1h. The verdict for each segment
| Shards stored | Result |
|---|---|
| 6 of 6 | Fine |
| 5 of 6 | Accepted, but the file will be marked **DEGRADED** and a `REPAIR_QUEUED` event is logged so the future scrubber can fix it |
| 4 or fewer | **Rollback.** The shards already uploaded are deleted, the file is marked FAILED, and you get **HTTP 503 "only 4/6 shards stored"** |

Why refuse at 4? With exactly 4 shards there is **zero safety margin**. One more failure and the file is lost forever. The gateway would rather say "no" than store your file unprotected. This is what happens when you upload with `minio-abroad` stopped.

### 1i. Done
Once all 3 segments are through, the file becomes **ACTIVE** (or DEGRADED) and an `UPLOAD` event is logged. You get back JSON with the file's `id`.

---

## Step 2: You download `marks.pdf`

You call `GET /files/{id}/download`.

### 2a. Check before sending anything
- Does the file exist, and is it ACTIVE or DEGRADED?
- Does **every segment have at least 4 shards** marked STORED?

If not, you get an error **immediately**, not a half-downloaded broken file.

### 2b. Unlock the file's key
The gateway takes the locked DEK from the database and unlocks it with the KEK held in RAM.

### 2c. Rebuild each segment, one at a time (`core/pipeline.py`)
For segment 0:
1. **Fetch 4 shards in parallel.** Only 4 are needed, so it doesn't waste time fetching all 6.
2. **Check each shard's SHA-256** against the fingerprint saved at upload time.
3. **Rebuild** the encrypted segment from the 4 shards (Reed–Solomon decode).
4. **Decrypt** with the DEK. AES-GCM checks the tag and the "address label" too.
5. **Send** those 4 MB to your browser immediately, then move on to segment 1.

Because it streams segment by segment, the download starts quickly and memory use stays small.

### 2d. When something goes wrong during download
| Problem | What the gateway does |
|---|---|
| A cloud is down and the fetch fails | Fetches one of the 2 spare shards instead. It also remembers "this cloud is dead" for the rest of this download and asks it last, so the download stays quick (about 4.6 s with `minio-abroad` stopped, versus 0.4 s normally) |
| A shard's SHA-256 doesn't match (bit rot or tampering) | Discards that shard, uses a spare, marks it **CORRUPT** in the database and logs a `CORRUPT` event |
| The decryption tag fails | Same treatment: try a different combination of shards, and log CORRUPT |
| Fewer than 4 good shards for a segment | Stops with an error. It never sends wrong data |

---

## Step 3: You delete `marks.pdf`

`DELETE /files/{id}`:
1. The file is immediately marked **DELETED**, and it vanishes from `GET /files` and can't be downloaded. You get **202 Accepted** right away.
2. **In the background**, the gateway deletes all 18 shards (3 segments × 6) from the clouds and removes their rows.
3. If a cloud is down at that moment, those rows are **kept**, so the future garbage collector can finish the job later.

---

## The events log: the gateway's diary

Every important moment is written to the `events` table:

| Event | When |
|---|---|
| `UPLOAD` | A file finished uploading |
| `REROUTE` | A shard was moved to a different cloud |
| `REPAIR_QUEUED` | A file is DEGRADED and needs fixing |
| `ROLLBACK` | An upload was cancelled and cleaned up |
| `CORRUPT` | A bad shard was found during download |
| `DELETE` | A file was deleted |

Later, the **dashboard** will show this diary and the **workers** will act on it. For example, the scrubber will pick up `REPAIR_QUEUED` and `CORRUPT` and rebuild those shards.

---

## What each party can see

| Who | What they hold | Can they read your file? |
|---|---|---|
| Each MinIO cloud | 2 of the 6 shards of each segment, with random names | No: the shards are ciphertext, and 2 aren't enough to rebuild a segment anyway |
| Postgres | The wrapped DEK, hashes and the shard map | No: the KEK isn't stored |
| The gateway while running | The KEK in memory | Yes, which is why the passphrase is needed at startup |

---

## Which file does what

| File | Role in the flow |
|---|---|
| `gateway/settings.py` | Loads `.env` (Step 0) |
| `gateway/service.py` | `unlock_kek` (Step 0); `FileService` runs upload, download and delete |
| `gateway/api/routes.py` | HTTP endpoints; startup wiring |
| `gateway/core/chunker.py` | 4 MB segments (1b) |
| `gateway/core/crypto.py` | Argon2id KEK, DEK, AES-GCM, DEK wrapping, HKDF (0, 1a, 1c, 2b) |
| `gateway/core/erasure.py` | RS(4,2) encode/decode, SHA-256 (1d, 2c) |
| `gateway/core/pipeline.py` | Ties encrypt + erasure together; download fallback (1c–1d, 2c–2d) |
| `gateway/placement/placer.py` | Placement plan, retries, re-routing, verdict (1e, 1g, 1h) |
| `gateway/providers/` | `base.py` interface, `s3_compat.py` MinIO client, `registry.py` loads `providers.yaml` |
| `gateway/db/models.py`, `repo.py` | Tables and all database reads/writes (1a, 1f, events) |
| `deploy/docker-compose.yml` | Postgres + 3 MinIO containers (+ `minio-extra` for tests) |
| `scripts/demo.py` | Live 7-step demo |
| `tests/` | 73 unit tests + 3 integration tests on real MinIO |

---

## The whole thing in one breath

> **Upload:** cut into 4 MB pieces → lock each piece with the file's own key → split each locked piece into 6 shards (any 4 rebuild it) → write down where each shard will go → send 2 shards to each cloud → if too many fail, undo everything.
>
> **Download:** check enough shards exist → unlock the file's key → for each piece, grab 4 shards, check their fingerprints, rebuild, unlock, send → if a shard is missing or bad, use a spare and note it.
>
> **Result:** any one cloud can **die** and you lose nothing; any cloud can **peek** and it sees only random bytes with random names; anyone can **tamper** and it's caught.

---

## Not built yet (next phases)
- **Workers:** scrubber (rebuilds CORRUPT/MISSING shards), garbage collector (cleans up after failed uploads), health checks, rebalancer
- JWT authentication
- Residency policies (`india_only`, `recoverable_in_india`, `any`)
- Hedged reads (`HEDGED_READS=false` by default)
- Cloudflare R2 and Google Drive providers
- Dashboard and benchmarks
