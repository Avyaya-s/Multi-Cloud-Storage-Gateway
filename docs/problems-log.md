# Problems Log

Real problems hit while building SovereignStripe: what we saw, why it happened, how we fixed it, and
what we learned. Add an entry whenever something breaks, surprises us, or forces a design change; the
"Challenges" chapter of the report is written from this file.

**Template**

```
## P<n>. <short title>  (<date>, <area>)
- Symptom:  what we actually observed
- Cause:    the root cause, once found
- Fix:      what we changed (file / commit)
- Lesson:   what this taught us about cloud systems
- Status:   fixed | workaround | open
```

---

## P1. Official MinIO Docker images could not be pulled  (Phase 2, deployment)
- Symptom: `docker pull minio/minio` and the quay.io mirror both failed, so the compose stack could not start.
- Cause: MinIO stopped publishing free community images in 2025 and moved features to its paid edition.
- Fix: switched to Chainguard's MinIO build, pinned by image digest in `deploy/docker-compose.yml`.
- Lesson: self-hosting does not remove vendor risk: the software supplier can change terms too. Pin
  images by digest so a moved tag cannot silently change what runs.
- Status: workaround

## P2. Port 5432 already in use  (Phase 2, deployment)
- Symptom: the Postgres container would not start; port 5432 was taken.
- Cause: a locally installed PostgreSQL Windows service already listened on 5432.
- Fix: compose maps Postgres to 127.0.0.1:5433; `.env` and the docs use 5433.
- Lesson: a developer laptop is a shared environment; never assume default ports are free.
- Status: fixed

## P3. Integration tests took 222 s instead of about a minute  (Phase 2, networking)
- Symptom: every new connection to MinIO stalled for seconds; the 3 integration tests took 222 s.
- Cause: on this Windows machine `localhost` resolves to IPv6 (::1) first; nothing listened there, so
  each connection waited for the IPv6 attempt to fail before falling back to IPv4.
- Fix: use `127.0.0.1` everywhere (providers.yaml, .env, tests, dashboard proxy), and rank shards on
  already-failed providers last during a download. Tests dropped to about 62 s.
- Lesson: measure before guessing. A name-resolution detail cost more time than all the crypto and
  erasure coding together. Recorded as a rule in CLAUDE.md.
- Status: fixed

## P4. A stopped container takes about 2 s to refuse each connection  (Phase 2, fault tolerance)
- Symptom: downloads with `minio-abroad` stopped took about 4.6 s instead of 0.4 s.
- Cause: on Windows, connecting to a stopped container's port is not refused instantly; the boto
  client also retried by default.
- Fix: short `connect_timeout` (2 s), a single attempt in botocore (our own retry logic decides), and
  per-download ranking so a provider that failed once is tried last for the rest of that download.
- Lesson: "down" is rarely instant in real networks; a failure can cost seconds, so the system must
  remember failures instead of paying for them again and again (motivation for health scoring).
- Status: workaround (health scoring will improve it)

## P5. One transient failure doomed every later segment of an upload  (Phase 2, placement)
- Symptom: with 3 providers, a single failed put marked the provider "bad", and every later segment
  had only 2 providers to use, so the whole upload rolled back.
- Cause: excluding failed providers is only safe while the remaining providers can still hold all 6
  shards under the 2-per-provider cap.
- Fix: `service._upload_segment` excludes failed providers only if `healthy * cap >= shards`.
- Lesson: a rule that is sensible with many providers can be harmful with few. Test with the minimum
  configuration.
- Status: fixed

## P6. Flaky DEGRADED tests  (Phase 2, testing)
- Symptom: the "one shard fails, file is DEGRADED" test passed sometimes and failed sometimes.
- Cause: the fake provider failed "the next N puts" with a shared counter. Uploads run in parallel
  with retries, so which shard consumed the failures varied from run to run.
- Fix: the fake now poisons one specific object key (`poison_first_key`), which fails on every retry.
- Lesson: concurrency makes counters non-deterministic; tests must fail a specific thing, not "the
  next N calls".
- Status: fixed

## P7. A rolled-back upload left rows behind  (Phase 2, consistency)
- Symptom: after a rollback with a provider down, the file was FAILED but some shard rows remained.
  We first thought this was a bug.
- Cause: the provider was unreachable, so the rollback could not confirm the delete; a put that
  errored might still have stored the object.
- Fix: none needed. This is the intended write-ahead behaviour: rows that may point at a real object
  are kept for the garbage collector. The test was corrected to expect it.
- Lesson: in distributed systems "I don't know whether it happened" is a real third outcome besides
  success and failure. Metadata must record what *might* exist, not only what surely exists.
- Status: by design (garbage collector cleans up)

## P8. Deleting files while clouds were down left orphaned shards  (dashboard testing, consistency)
- Symptom: after deleting two PDFs, 16 objects were still in the `minio-in-1` and `minio-abroad`
  buckets, and their shard rows stayed in the database.
- Cause: those containers were stopped at the moment of the delete, so the background purge could not
  remove the objects.
- Fix: rows are kept (same reasoning as P7) and the garbage collector retries. On 2026-11-19 its
  first real run deleted all 18 leftover objects (16 from the PDFs, 2 rows from `new.txt`) and
  removed the 3 dead files in 0.2 s; each bucket was left with exactly the 6 shards of the one
  live file.
- Lesson: deletes in multi-cloud storage are not atomic; "delete" must be a retried process, not a
  single call.
- Status: fixed (garbage collector)

## P9. A cloud that is down at gateway startup stays DOWN until restart  (dashboard testing, health)
- Symptom: after starting a stopped MinIO container, the dashboard kept showing it as DOWN.
- Cause: `open_all()` logs a provider that fails to open and moves on; nothing reopens it later, so
  its client stays `None` and every probe fails.
- Fix: planned with health checks (reconnect on probe).
- Lesson: startup is just one moment; components must recover from failures that happen at any time,
  including before they started.
- Status: open (health scoring)

## P11. Scrubber repaired the shard but never marked the file healed  (scrubber, 2026-11-19)
- Symptom: in the test "DEGRADED upload is healed", the report said `repaired: 1` but the file stayed
  DEGRADED and `healed_files` was empty.
- Cause: `_repair_shard` removes each fixed shard from the `bad` set, and the caller then compared
  `repaired == len(bad)`, a set that had just shrunk to 0, so 1 == 0 and the segment looked incomplete.
- Fix: snapshot the list to repair first and return `not bad` at the end (`gateway/workers/scrubber.py`).
- Lesson: mutating a collection that the caller also uses for bookkeeping is an easy bug to write; the
  test was written for the exact outcome (file back to ACTIVE), not just "something was repaired".
- Status: fixed

## P12. Design questions the scrubber forced us to answer  (scrubber, 2026-11-19)
- A provider that does not answer: data loss or not? We chose NOT. Its shards are marked
  "unreachable" and left alone; otherwise a 2-minute outage would trigger a full rebuild of a cloud's
  data. Moving data off a persistently failing provider is the rebalancer's job.
- Repair needs no key: lost shards are rebuilt from ciphertext with Reed-Solomon, and each rebuilt
  shard must match its SHA-256 from upload time before it is stored. The scrubber can run even if
  the gateway were locked.
- A quick scrub (HEAD) cannot see bit rot (object exists, bytes wrong); only a deep scrub (download +
  SHA-256) can, at the cost of downloading everything (egress on real clouds). Hence two schedules:
  quick every 5 min, deep every hour.
- Moving a shard to another provider leaves the old copy behind. It is recorded in a new `orphans`
  table BEFORE the shard row is re-pointed (write-ahead), so no object is ever untracked.
- With 3 providers every cloud already holds 2 shards per segment, so a shard can only be repaired
  on its own provider; if that provider refuses writes, the shard stays MISSING until it recovers.
  A 4th provider gives repairs somewhere else to go.
- Open: a file deleted while a repair is mid-upload can leave one untracked object (the scrubber
  re-checks the file status before writing, which narrows but does not close the window).

## P13. Rows with no provider kept dead files alive forever  (garbage collector, 2026-11-19)
- Symptom: found while designing the GC. A rolled-back upload with nowhere to place some shards
  (e.g. only 2 providers) left a FAILED file row that could never be removed.
- Cause: shard rows with `provider_id = NULL` point at nothing, so no delete ever "confirms" them,
  and `forget_shards` only removes the file once no shard rows remain.
- Fix: `forget_shards` also drops provider-less rows. A test reproduces it; with the old code the
  test fails, with the fix it passes (checked by temporarily restoring the old function).
- Lesson: cleanup logic must handle every state a row can be in, including "never placed".
- Status: fixed

## P14. Garbage collection design decisions  (garbage collector, 2026-11-19)
- Metadata rows are removed only AFTER the provider confirms the delete; a down provider keeps its
  rows until a later run. Deleting a key that is already gone is not an error, so every run is safe
  to repeat.
- Stale uploads: a file still PENDING an hour after it started is treated as crashed and cleaned.
  Risk: a genuinely slow upload. Guard: finishing an upload now only succeeds if the file is still
  PENDING, so an abandoned upload is rolled back instead of resurrected.
- The sweep (delete objects no row mentions) is the only step that can destroy data that metadata
  does not know about, so it: lists the bucket BEFORE reading known keys (write-ahead means every
  real object already has a row), ignores objects younger than 1 hour, runs only once a day, and
  assumes each bucket belongs to this gateway alone. Two gateways with different databases on one
  bucket would delete each other's data. Not run on the real data without asking.
- This also closes P12's delete-during-repair window: any untracked object it leaves is swept.

## P10. Small tooling surprises  (Phases 1-2)
- `python-dotenv` crashed (`AssertionError` in `find_dotenv`) when a script was fed through stdin:
  load `.env` from an explicit path instead of searching for it.
- `pytestmark` placed in `conftest.py` had no effect: pytest applies it per test module only.
- A test checking that a secret never appears in a repr used the secret `"p"`, which matched inside
  the word `S3CompatProvider`: secrets in tests must be distinctive strings.
- The dashboard test gateway would not start because a teammate's older gateway already held port
  8000; tested on 8001 instead of killing someone else's process.
- Lesson: most lost time comes from the environment, not the algorithm.
