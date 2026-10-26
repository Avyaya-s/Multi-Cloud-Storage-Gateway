# SovereignStripe – Detailed Project Report

## 1. Executive summary

We are building a storage gateway that encrypts files, splits them into shards, and spreads the shards across three clouds so that losing any one cloud loses no data and exposes nothing.

- **What it is:** a gateway (Python/FastAPI) that sits between users and several storage providers: MinIO (self-hosted), Cloudflare R2 and Google Drive.
- **How it protects data:** every file is encrypted before it leaves the gateway (AES-256-GCM, envelope encryption), then Reed–Solomon RS(4,2) coding turns each 4 MB piece into 6 shards, any 4 of which rebuild it.
- **Why it matters:** it survives a full cloud outage at 1.5× storage cost, versus 3× for keeping full copies, and no single cloud ever holds readable data.
- **Extension:** placement policies that keep sensitive files recoverable within India and move shards away from unhealthy clouds.
- **Cost:** ₹0, using free tiers (Cloudflare R2, Google Drive, Azure for Students) and local Docker containers.
- **Course fit:** covers cloud storage and standards and security (Unit III), SOA (Unit II), virtualization/containers (Unit IV), and application management (Unit V).

## 2. Background and problem

Storing everything with one cloud provider creates four risks that no single provider can remove on its own.

| Risk | What happens | Does full replication fix it? | Does our gateway fix it? |
| --- | --- | --- | --- |
| Outage | Provider goes down, data unreachable | Yes | Yes, any 4 of 6 shards rebuild each piece |
| Lock-in | Leaving is slow and costly | Partly | Yes, providers are interchangeable adapters |
| Trust / breach | Provider or attacker can read data | No | Yes, only encrypted shards leave the gateway |
| Storage cost | — | No, 3× storage | Yes, 1.5× storage |
| Data residency | Law may require data to stay in India | No | Extension: recoverable-in-India placement |

India's Digital Personal Data Protection (DPDP) Act is why residency is on the list: it makes where personal data lives a compliance question, not just a technical one.

## 3. Objectives and scope

The core must be finished; the extension is attempted once the core works; everything else is explicitly out of scope.

**Objectives (as in the synopsis)**

1. **Secure storage:** client-side envelope encryption with authenticated, segment-wise AES-256-GCM.
2. **Fault tolerance:** RS(4,2) erasure coding across independent providers; survive the loss of any one provider.
3. **Self-healing:** detect and rebuild missing or corrupted shards automatically.
4. **Policy-based placement:** explore residency-aware placement and health-driven migration.
5. **Evaluation:** measure performance, fault tolerance, repair time and storage cost; compare with full replication.

**Scope**

| Tier | Includes |
| --- | --- |
| Core (must finish) | Upload, download, list, delete · encryption · RS(4,2) across 3 providers · metadata DB · download with one provider down · basic scrubber |
| Extension (attempt) | Health scoring and circuit breaker · residency policies (india\_only, recoverable\_in\_india, any) · rebalancer that migrates shards · dashboard |
| Out of scope | Full S3 API compatibility · mounting as a filesystem · multi-user sharing and permissions · production key management (HSM/KMS) |

## 4. Key concepts in plain language

The whole project rests on three ideas: scramble the data, split it with backups, and spread the pieces carefully.

**Encryption: "a letter in a locked box, the box key in a safe"**

- Each file gets its own random **file key (DEK)**. Each 4 MB piece is scrambled with it using **AES-256-GCM**.
- The scrambling machine also adds a 16-byte **tamper seal (auth tag)**; change one byte and unlocking fails.
- Each piece uses a **unique number (nonce)** = its piece number, and a **label (AAD)** = file ID + piece number + "last piece" flag, so pieces cannot be swapped, reordered or chopped off the end.
- The file key is itself locked (**wrapped**) with the **master key (KEK)**, made from the admin password by a deliberately slow mixer (**Argon2id**). Only the locked file key is stored.
- An attacker needs three things at once: the master password, the metadata database, and at least 4 shards.

**Erasure coding: "two equations, two unknowns"**

- Each scrambled piece is cut into 4 **data shards**, and 2 **parity shards** are calculated from them with two different recipes.
- Two recipes give two equations, so any 2 missing shards can be solved for: **any 4 of 6 rebuild the piece**.
- Real Reed–Solomon uses **Galois field GF(256)** arithmetic so every result fits in one byte; the zfec library does the maths.
- Storage cost: 6 MB stored per 4 MB of data = **1.5×**.

**Placement: "never more than 2 eggs in one basket"**

- Each cloud holds **at most 2 shards of any piece**, so losing a whole cloud leaves 4, which is enough.
- Extension: sensitive files keep **at least 4 shards in India** (rebuildable at home; the ≤2 abroad can never rebuild it), and shards move off clouds whose health score drops.

**Glossary**

| Simple name | Technical term |
| --- | --- |
| Piece | Segment (4 MB) |
| File key | DEK – Data Encryption Key |
| Master key | KEK – Key Encryption Key |
| Slow password mixer | Argon2id |
| Scrambling machine | AES-256-GCM |
| Unique number per piece | Nonce |
| Label glued to the lock | AAD – Associated Data |
| Tamper seal | Authentication tag |
| Locking / unlocking the file key | Wrapping / unwrapping |
| Backup shards | Parity shards |
| Rebuilding from shards | Erasure decoding |
| Chopping pieces off the end | Truncation attack |
| Fingerprint of a shard | SHA-256 hash |

## 5. System architecture

Everything runs inside one gateway service; the clouds only ever receive encrypted shards, and the metadata database is the only place that knows where they are.

&#91;embedded content: system architecture · 7 gateway components, 3 storage providers\]

The data pipeline (highlighted) is the heart of the system; everything else serves it, protects it or keeps its output healthy.

| Component | Responsibility | Tier |
| --- | --- | --- |
| API layer | Upload, download, list, delete, admin and fault-injection endpoints; JWT auth | Core |
| Data pipeline | Cut into 4 MB pieces → encrypt (AES-256-GCM) → RS(4,2) code; reverse on download | Core |
| Placement engine | Decides which cloud gets which shard; max 2 per piece per cloud; residency rules | Core (basic) / Extension (policies) |
| Provider adapters | One interface (put, get, head, delete, probe) over MinIO, R2, Google Drive | Core |
| Metadata DB | Shard map, SHA-256 fingerprints, wrapped file keys, file status | Core |
| Health monitor | EWMA score from latency, success rate and cost; circuit breaker | Extension |
| Workers | Scrubber (repair), rebalancer (migrate), garbage collector (cleanup) | Scrubber core; rest extension |

## 6. Detailed workflows

Four flows run the system: upload and download on request, repair and rebalancing in the background.

**Upload**

1. User logs in (JWT). The master key (KEK) is already in memory, derived from the admin password at startup.
2. User uploads a file and picks a policy (for example `sensitive` or `standard`).
3. File is cut into 4 MB pieces; the last piece may be smaller.
4. A random 256-bit file key (DEK) and a 4-byte nonce prefix are generated for the file.
5. Each piece is encrypted: nonce = prefix + piece number; AAD = file ID + piece number + last-piece flag → ciphertext + 16-byte tag.
6. Each encrypted piece is RS(4,2) coded into 4 data + 2 parity shards; each shard gets a SHA-256 fingerprint and a random UUID name.
7. Placement engine assigns shards: at most 2 per cloud per piece, residency rule, healthier clouds first.
8. Metadata rows are written with status `PENDING` (write-ahead).
9. Shards upload in parallel (asyncio, \~16 at a time), with 3 retries and re-routing to another valid cloud on failure.
10. File key is wrapped with the KEK and stored; file marked `ACTIVE` (all 6 shards) or `DEGRADED` (≥5, repair queued); unwrapped key erased from memory.

**Download**

1. Look up the file's shard map and wrapped key; unwrap the file key with the KEK (wrong KEK → fails immediately).
2. For each piece, request 4 shards from the healthiest clouds; if one is slow, request a 5th (hedged read) and take the first 4.
3. Verify each shard's SHA-256; a mismatch is treated as missing and logged as `CORRUPT`.
4. Erasure-decode to the encrypted piece, rebuild nonce and AAD, decrypt; tag failure → try other shards.
5. Check the piece count matches the database, stream the file, erase the file key.

**Repair (scrubber)**

1. Periodically check shards: cheap HEAD requests for existence, full hash checks on a random sample.
2. For a missing or corrupt shard: fetch any 4 healthy shards of that piece, rebuild the piece, re-compute just that shard.
3. Upload it to the same cloud if healthy, else another valid cloud; update the shard map. Piece is back to 6/6.

**Rebalancing (extension)**

1. Health monitor updates each cloud's score every 15–30 s from latency, success rate and cost.
2. If a cloud stays below 0.5 for longer than a dwell time (\~5 min), its shards are scheduled to move.
3. Each shard is copied (or rebuilt, if unreadable) to a valid healthier cloud, verified, the map updated, the old copy deleted.
4. Migrations are rate-limited; a cloud is trusted again only above 0.7 (hysteresis prevents flip-flopping).

**Failure behaviour at a glance**

| Shards lost per piece | Result |
| --- | --- |
| 0 | Normal |
| 1–2 (e.g. one whole cloud down) | Download still works; scrubber restores missing shards |
| 3 or more (two clouds down at once) | That piece can't be rebuilt until a cloud returns; clear error, never a corrupted file |

## 7. Data model and API design

Six tables hold everything the gateway knows; the clouds hold only anonymous encrypted shards.

**Tables**

| Table | Key columns | Purpose |
| --- | --- | --- |
| providers | id, name, type, endpoint, bucket, jurisdiction (IN / NON\_IN), cost\_per\_gb, enabled | Registered clouds |
| provider\_stats | provider\_id, ewma\_latency\_ms, ewma\_success, score, breaker\_state, updated\_at | Live health |
| files | id, name, size, content\_hmac, policy, wrapped\_dek, nonce\_prefix, status, created\_at | One row per file |
| segments | id, file\_id, seg\_index, plain\_len, k, m | One row per 4 MB piece |
| shards | id, segment\_id, shard\_index, provider\_id, object\_key (UUID), sha256, size, status | Where every shard lives |
| events | id, ts, type, details\_json | Upload, repair, migrate, breaker events for the dashboard |

Design notes:

- Shard names are random UUIDs, so a cloud learns nothing about file names or structure.
- Files store an HMAC of the content, not a plain SHA-256, so nobody can confirm whether a known file is stored.
- File status: `PENDING` → `ACTIVE` / `DEGRADED` → `DELETED`.

**Provider adapter interface**

```python
class StorageProvider(ABC):
    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def head(self, key: str) -> bool: ...
    async def delete(self, key: str) -> None: ...
    async def probe(self) -> float: ...   # latency in ms
```

One S3-compatible adapter (aioboto3 + endpoint URL) covers MinIO and Cloudflare R2; Google Drive needs its own adapter because Drive addresses files by ID, not by path.

**API endpoints**

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | /files | Upload (multipart) with a policy |
| GET | /files | List files |
| GET | /files/{id} | Metadata and status |
| GET | /files/{id}/download | Streamed download |
| GET | /files/{id}/placement | Shard map for the dashboard |
| DELETE | /files/{id} | Soft delete; garbage collector removes shards |
| GET | /providers | Scores, breaker state, jurisdiction |
| POST | /admin/faults/{provider} | Inject latency or outage (demo) |
| POST | /admin/scrub, /admin/rebalance | Trigger workers manually |
| GET | /metrics | Prometheus metrics |

## 8. Technology stack, infrastructure and cost

The whole project can be built and demoed for ₹0; the core runs entirely on one laptop with Docker.

**Stack**

| Layer | Choice |
| --- | --- |
| Gateway and APIs | Python, FastAPI, asyncio |
| Cryptography | cryptography (AES-256-GCM), argon2-cffi (Argon2id) |
| Erasure coding | zfec (Reed–Solomon, easyfec module) |
| Storage access | aioboto3 (S3-compatible), google-api-python-client (Drive) |
| Metadata | PostgreSQL + SQLAlchemy (SQLite acceptable while prototyping) |
| Scheduling | APScheduler for background workers |
| Fault injection | Toxiproxy |
| Dashboard | React (Vite) + Recharts |
| Testing and benchmarks | pytest, Hypothesis, Locust, Prometheus client |
| Deployment | Docker, Docker Compose |
| Version control | Git / GitHub |

**Providers and cost**

| Role | Provider | Cost | Notes |
| --- | --- | --- | --- |
| Cloud 1 | MinIO (self-hosted, Docker) | Free | Also used as 3 fake clouds during development |
| Cloud 2 | Cloudflare R2 | Free tier | 10 GB storage, free egress; same S3 code as MinIO |
| Cloud 3 | Google Drive | Free (15 GB) | Needs OAuth / service-account setup; swap for another S3-compatible store if it becomes a time sink |
| Gateway host | Laptop, or Azure for Students VM | Free | USD 100 credit, no card; services stop instead of charging |
| AWS (optional) | S3 | Credit-based, card needed | Only if the guide wants AWS; set a budget alert on day one |

**docker-compose services:** gateway, postgres, minio-in-1, minio-in-2, minio-abroad, toxiproxy, dashboard (optional: prometheus, grafana). India / abroad are configuration labels on each provider for the demo.

## 9. Implementation plan, timeline and team roles

The plan puts a complete, demoable core at the end of week 4, so the extension work is a bonus rather than a risk.

&#91;embedded content: build plan · 6 work packages over 7 weeks\]

Weeks are relative to the start date; stretch or compress them to fit the actual submission deadline, which is not yet set in this plan.

**Milestone checks**

- [ ] W2: a file round-trips (encrypt → 6 shards → delete 2 → rebuild → decrypt) byte-for-byte on 3 MinIO containers
- [ ] W3: upload/download API works end to end with the metadata DB
- [ ] W4: stopping one MinIO container still allows downloads; scrubber restores the missing shards (**core complete**)
- [ ] W5: health scores and residency policies visible via /providers
- [ ] W6: rebalancer moves shards off a Toxiproxy-slowed provider; dashboard shows it
- [ ] W7: R2 and Google Drive connected; benchmarks run; report drafted

**Team roles (4 people)**

| Role | Owns |
| --- | --- |
| Core engine | Chunking, encryption, erasure coding, decode logic, crypto tests |
| Providers and health | MinIO / R2 / Drive adapters, health monitor, circuit breaker |
| Metadata and workers | Database schema, FastAPI endpoints, scrubber, rebalancer, garbage collector |
| Dashboard, deployment, evaluation | Dashboard UI, Docker Compose, Toxiproxy scenarios, benchmarks, report graphs |

## 10. Testing and evaluation plan

Tests prove the system is correct; experiments prove how well it works, with every result backed by a measured number.

**Tests**

| Type | What it checks |
| --- | --- |
| Property-based (Hypothesis) | For random files and every combination of 4 shards out of 6, decoding returns the original bytes exactly |
| Tamper | Flipping one byte in a shard or ciphertext is caught (hash mismatch or GCM tag failure) |
| Truncation / reorder | Dropping the last piece, or swapping two pieces, fails decryption |
| Placement rules | Never more than 2 shards per piece per cloud; residency rules hold; impossible placements give a clear error |
| Integration (Docker) | Upload → stop a container → download → restart → scrubber repairs |
| Chaos | Random Toxiproxy faults during uploads and downloads; zero data loss |

**Experiments**

| # | Experiment | Metric | Tier |
| --- | --- | --- | --- |
| 1 | RS(4,1), RS(4,2), RS(6,3) vs 3× replication | Storage overhead, failures tolerated | Core |
| 2 | File sizes 1 MB to 500 MB | Upload / download throughput | Core |
| 3 | One provider down | Download latency, success rate | Core |
| 4 | Provider restored after outage | Time to full repair | Core |
| 5 | Hedged vs normal reads | p99 latency | Core |
| 6 | 500 ms latency injected into one provider | Static vs adaptive placement: p50/p95 latency over time | Extension |
| 7 | All non-India providers blocked | Sensitive files still rebuild | Extension |
| 8 | Per-file policies vs one-size-fits-all | Projected monthly cost per 100 GB | Extension |

If the extension is not finished, experiments 1–5 alone still give a complete evaluation section.

## 11. Risks and mitigations

The biggest risk is scope creep, not technical difficulty; every other risk has a cheap fallback.

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Scope creep (S3 compatibility, filesystem mount, sharing) | Core not finished | Hold to the core list; extension only after the W4 gate |
| Extension not finished in time | Weaker novelty story | Synopsis already frames it as an extension; core + experiments 1–5 stand alone |
| Google Drive OAuth / rate limits | Days lost on setup | Replace with Backblaze B2 or a second R2 bucket (S3-compatible, same adapter) |
| Nonce reuse or crypto misuse | Security broken | Use the cryptography library only; nonce from piece number; unit tests for tamper and truncation |
| Partial upload failure | Orphaned shards, inconsistent state | Write-ahead PENDING rows, retries, re-routing, garbage collector |
| Metadata DB is a single point of failure | Shard map lost = data unreadable | Nightly encrypted, erasure-coded backup of the DB stored across the providers |
| Master password lost | All files unreadable | Documented backup of the admin passphrase; state this trade-off in the report |
| Surprise cloud bills | Money | Stay on free tiers; if AWS is used, budget alert on day one and stop idle VMs |
| Large files exhaust memory | Crashes | Stream 4 MB pieces; never load a whole file |
| Examiner challenges novelty | Viva difficulty | Cite RACS, DepSky, Storj, Scalia; claim only the specific extension and the evaluation |

## 12. Related work and positioning

The building blocks are established; our contribution is a working, measured implementation plus a specific placement extension, not a new storage paradigm.

| Work | What it does | How we relate |
| --- | --- | --- |
| RACS (2010) | RAID-like striping across cloud providers to avoid lock-in | Same core idea; we add encryption and self-healing |
| DepSky (2011) | Encrypted, erasure-coded storage across multiple clouds | Closest ancestor; cite it |
| NCCloud (2012) | Cheaper repair of lost shards across clouds | Related to our scrubber |
| Scalia (SC'12) | Adapts placement across clouds, mainly to cut cost from access patterns | Our rebalancer reacts to health (latency, errors) instead |
| Tahoe-LAFS | Encrypt-then-erasure-code, the same order we use | Validates our pipeline order |
| Storj | Commercial erasure-coded storage; geofencing keeps all pieces in one region (e.g. EU for GDPR) | Our residency rule is softer: ≥4 shards in India, ≤2 abroad |

**What we claim (and only this)**

- A lightweight, open implementation over ordinary public clouds, built from standard components.
- A controlled comparison of erasure coding against replication, with measured availability, latency, repair time and cost.
- As an extension: a recoverable-in-India placement rule and health-driven shard migration.

**Standards our crypto follows:** NIST SP 800-38D (AES-GCM), RFC 9106 (Argon2), envelope encryption as used by AWS KMS / Google Cloud KMS / Azure Key Vault, and the STREAM construction (Hoang, Reyhanitabar, Rogaway and Vizár, 2015) for segment-wise encryption with truncation protection.

- [ ] Before submission: search Google Scholar for "multi-cloud erasure coding data residency", "jurisdiction-aware data placement" and "sovereignty-aware cloud storage"; narrow the extension claim if the exact rule already exists.

## 13. Demo plan and viva preparation

The demo shows one idea per step, and the strongest moment is a cloud being killed live while the download still works.

**Demo script (about 5 minutes)**

1. Upload a PDF; the dashboard shows its shards spread over 3 providers, 2 per provider.
2. Open a raw shard in the MinIO or R2 console: it is unreadable ciphertext with a random name.
3. `docker stop` one MinIO container; that provider turns red; the PDF still downloads.
4. Corrupt one shard by hand; the hash check catches it and the file still decodes.
5. Restart the container; the scrubber rebuilds the missing shards and everything goes green.
6. (Extension) Slow one provider with Toxiproxy; watch the rebalancer move shards off it.

**Likely viva questions**

| Question | Short answer |
| --- | --- |
| Why encrypt before erasure coding? | Data shards are plain slices of the input; coding first would leave readable pieces. Encrypting first means every shard is ciphertext. |
| Why two keys (DEK and KEK)? | A leaked file key exposes one file; changing the master password only re-wraps small keys instead of re-encrypting everything. |
| What if the same nonce is reused? | AES-GCM security breaks. We derive the nonce from the piece number and use a fresh key per file, so it never repeats. |
| How can any 4 of 6 shards rebuild the data? | The 2 parity shards come from 2 different Reed–Solomon equations over GF(256); 2 equations solve 2 unknowns. |
| Why RS(4,2) and not replication? | Same single-provider tolerance at 1.5× storage instead of 3×, and no provider holds a full copy. |
| What if two clouds fail at once? | Pieces with 3+ lost shards cannot be rebuilt until one returns; the system reports an error rather than returning bad data. RS(4,3) would tolerate more at higher cost. |
| What is the single point of failure? | The metadata DB; mitigated by an encrypted, erasure-coded backup stored across the providers. |
| What stops truncation attacks? | A last-piece flag in the AAD plus a piece-count check against the database. |
| Isn't this just Storj / DepSky? | The core idea is theirs and we cite them; our contribution is the working implementation, the measured comparison and the placement extension. |

## 14. Deliverables and references

**Deliverables**

- [ ] Synopsis (LaTeX, RVCE format)
- [ ] Source code on GitHub with a README and one-command Docker Compose setup
- [ ] Test suite (unit, property-based, integration, chaos)
- [ ] Benchmark scripts and result graphs
- [ ] Final report: Motivation → Design → Method → Evaluation → Results
- [ ] Live demo following the script in section 13

**References**

- H. Abu-Libdeh, L. Princehouse, H. Weatherspoon, "RACS: A Case for Cloud Storage Diversity", ACM SoCC, 2010.
- A. Bessani et al., "DepSky: Dependable and Secure Storage in a Cloud-of-Clouds", EuroSys, 2011.
- Y. Hu et al., "NCCloud: Applying Network Coding for the Storage Repair in a Cloud-of-Clouds", USENIX FAST, 2012.
- T. G. Papaioannou, N. Bonvin, K. Aberer, "Scalia: An Adaptive Scheme for Efficient Multi-Cloud Storage", SC'12, 2012 — [ACM](https://dl.acm.org/doi/abs/10.5555/2388996.2389024)
- V. T. Hoang, R. Reyhanitabar, P. Rogaway, D. Vizár, "Online Authenticated-Encryption and its Nonce-Reuse Misuse-Resistance", CRYPTO, 2015 (STREAM construction).
- NIST SP 800-38D, Recommendation for Block Cipher Modes of Operation: GCM and GMAC.
- RFC 9106, Argon2 Memory-Hard Function for Password Hashing.
- Storj geofencing for GDPR — [storj.io](https://www.storj.io/object-storage/compliance/gdpr)
- Tahoe-LAFS documentation (encrypt-then-erasure-code design).
- Cloudflare R2 free tier — [Nubbo](https://nubbo.app/blog/cloudflare-r2-free-tier/); Azure for Students — [Microsoft](https://azure.microsoft.com/en-in/free/students)

Paper details other than the Scalia, Storj, R2 and Azure links are from memory; verify authors and venues on Google Scholar before citing them in the report.
