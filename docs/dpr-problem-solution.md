# SovereignStripe – Project Report: Problem, Idea and Solution

## 1. Overview

SovereignStripe is a storage gateway that lets people keep files across several clouds so that no single cloud can lose, lock in, or read their data.

Today most people and organisations put all their files with one provider. When that provider goes down, raises prices, or is breached, the data is stuck or exposed. Our system sits between the user and three storage providers. It scrambles every file before it leaves, cuts each piece into six shards with built-in backups, and spreads them so that each cloud holds only two. Any one cloud can disappear completely and every file is still recoverable, while no cloud ever holds anything readable.

The project is built for the Cloud Computing elective (IS355TBD) and maps onto its syllabus: cloud storage providers and standards, cloud security, service-oriented architecture, virtualization and application management.

## 2. Target users

The system targets anyone who keeps important files in the cloud but cannot afford to lose them, leak them, or be stuck with one provider.

| User | Example | What they need most |
| --- | --- | --- |
| Small and medium businesses | A clinic, a CA firm, a startup | Keep client records safe and available without an enterprise budget |
| Educational institutions | Colleges storing exam papers, results, student records | Availability during peak times; confidentiality of records |
| Organisations handling personal data | Healthcare, fintech, HR platforms | Compliance with India's DPDP Act; data recoverable within India |
| Developers and IT teams | Teams building apps on cloud storage | Avoid vendor lock-in; a single interface over many providers |
| Individuals with critical files | Researchers, professionals | A cheap, private backup that survives a provider shutting an account |

## 3. Problems

Storing everything with one cloud provider creates five problems, and the usual fix, keeping extra copies, solves only one of them.

**P1. Outages make data unreachable.** Even the largest providers have region-wide outages. During one, every file stored there is simply unavailable, often at the worst possible time.

**P2. Vendor lock-in.** Once terabytes live with one provider, leaving is slow and expensive: data must be downloaded, transferred and re-uploaded, and the application rewritten for a new API. Price rises or policy changes must simply be accepted.

**P3. The provider can read the data.** Files stored normally are readable by the provider, and by anyone who breaches the provider or steals account credentials. The user has to trust someone else with everything.

**P4. Replication is expensive and still leaky.** The common fix is to keep full copies with two or three providers. That survives outages but triples storage cost, and now three providers can read the data instead of one.

**P5. Data-residency rules.** India's Digital Personal Data Protection (DPDP) Act makes where personal data lives a legal question. Spreading copies to foreign clouds for safety can conflict with keeping sensitive data under Indian jurisdiction.

| Problem | Single cloud | Full replication (3 copies) |
| --- | --- | --- |
| P1 Outage | Not solved | Solved |
| P2 Lock-in | Not solved | Partly solved |
| P3 Provider can read data | Not solved | Worse: 3 providers can read it |
| P4 Storage cost | 1× | 3× |
| P5 Residency | Depends on provider region | Harder: copies spread abroad |

## 4. Ideas considered

We compared five ways of protecting cloud data; only encryption combined with erasure coding across clouds solves all five problems at a reasonable cost.

| Idea | How it works | Strength | Why not enough on its own |
| --- | --- | --- | --- |
| Better single provider | Pick the most reliable cloud, use its multi-region option | Simple | Still lock-in; provider can still read data |
| Encrypt, then single cloud | Scramble files before upload | Solves privacy | Still one point of failure and lock-in |
| Full replication | A complete copy with 2–3 providers | Survives outages | 3× cost; every provider holds a full copy |
| Encrypted replication | Encrypt, then copy to 2–3 providers | Private and available | Still 3× cost |
| **Encryption + erasure coding across clouds (chosen)** | Encrypt, split into 6 shards with 2 backups, spread 2 per cloud | Private, survives any one cloud, only 1.5× cost | More complex to build, which is the engineering challenge of the project |

Within the chosen idea we also weighed how to split the data:

- **RS(4,1):** cheapest (1.25×) but survives only 1 lost shard, not a whole cloud holding 2.
- **RS(4,2):** survives any 2 lost shards, so a whole cloud, at 1.5×. **Chosen as the default.**
- **RS(6,3):** survives 3 lost shards at 1.5× but needs more providers to spread well; kept as an option for critical files.

## 5. Existing solutions and their gaps

The core idea has been explored in research and industry; the gaps are in cost-effective, easy-to-run versions and in residency-aware placement.

| Existing solution | What it does | Gap |
| --- | --- | --- |
| Cloud provider multi-region storage (e.g. S3 cross-region replication) | Copies data across regions of one provider | Still one provider: lock-in, and the provider can read the data |
| RACS (research, 2010) | Stripes data across providers like RAID across clouds | Research prototype; focus on lock-in, not a full encrypted, self-healing system |
| DepSky (research, 2011) | Encrypted, erasure-coded storage across multiple clouds | Research system; no residency rules or health-based placement |
| Scalia (research, 2012) | Moves data between clouds to cut cost based on access patterns | Optimises cost, not reaction to failing providers |
| Tahoe-LAFS (open source) | Encrypts then erasure-codes files across storage servers | Built for its own server grid, not ordinary public clouds; complex to set up |
| Storj (commercial) | Encrypts and erasure-codes data across thousands of nodes; geofencing keeps all pieces in one region | Its own network, not your choice of clouds; geofencing is all-or-nothing |

**Gaps our project targets**

- A simple gateway that works over the clouds a user already has, including free tiers and self-hosted storage.
- Automatic self-healing, so failures do not pile up over time.
- A middle ground for residency: sensitive data always recoverable inside India, while still gaining outage protection from foreign clouds.
- A clear, measured comparison of this approach against replication.

## 6. Proposed solution

We propose a gateway that scrambles every file, splits each piece into six shards with two backups, and spreads them two per cloud, so any one cloud can vanish without loss and none can read anything.

&#91;embedded content: proposed solution · scramble, split into 6, spread 2 per cloud\]

The splitting step (highlighted) is what turns three ordinary clouds into one reliable store; the metadata database remembers where every shard went.

**How it answers each problem**

| Problem | How the solution answers it |
| --- | --- |
| P1 Outage | Each cloud holds 2 of 6 shards; losing one leaves 4, enough to rebuild |
| P2 Lock-in | Clouds plug in through one common interface; any can be swapped |
| P3 Provider can read data | Files are scrambled before leaving; each cloud holds only 2 scrambled fragments |
| P4 Storage cost | 6 shards per 4 pieces of data = 1.5×, half the cost of 3 copies |
| P5 Residency (extension) | Sensitive files keep at least 4 shards on Indian clouds, so they can always be rebuilt in India |

## 7. How the solution works

The solution rests on three ideas: scramble the data, split it with backups, and spread the pieces so no cloud holds too many.

**Idea 1: Scrambling (encryption)**

Think of a letter locked in a box, with the box key kept in a safe, and the safe combination kept only in your head.

1. The file is cut into pieces of 4 MB, so even huge files are handled one piece at a time.
2. A random **file key** is created just for this file. Every file gets a different one.
3. Each piece is scrambled with the file key using **AES-256-GCM**, the standard used to secure most web traffic. The output looks like random noise.
4. Every scrambled piece carries a small **tamper seal**. If anyone changes even one byte, the seal breaks and the system refuses to unscramble it.
5. Each piece is also labelled with its file and position (and whether it is the last piece), so pieces cannot be swapped, reordered or dropped without detection.
6. The file key is itself locked with a **master key**, made from an admin password by a deliberately slow process (**Argon2id**) that makes password guessing impractical. Only the locked file key is saved.

**Idea 2: Splitting with backups (erasure coding)**

Think of two equations with two unknowns: as long as you have two equations, you can always find two missing values.

1. Each scrambled piece is cut into **4 data shards**.
2. Two extra **backup shards** are calculated from those 4 using two different mathematical recipes (**Reed–Solomon coding**).
3. Because there are two recipes, any two missing shards can be solved for. **Any 4 of the 6 shards rebuild the piece.**
4. Storage cost: 6 shards for every 4 shards of data = **1.5×**, compared with 3× for keeping three full copies.

**Idea 3: Spreading the shards (placement)**

Think of never putting more than two eggs in one basket.

1. Each cloud receives **at most 2 shards of any piece**. With 3 clouds, each gets exactly 2.
2. If a whole cloud disappears, each piece loses only 2 shards, leaving 4, which is enough.
3. The metadata database records where every shard is, a fingerprint of each shard, and the locked file key.
4. As an extension, **sensitive files keep at least 4 shards on Indian clouds**, so they can always be rebuilt in India, and the 2 abroad can never rebuild them alone. The system can also **move shards away from clouds that are becoming slow or unreliable**, before they fail.

**Getting a file back**

1. The locked file key is unlocked with the master key.
2. For each piece, any 4 healthy shards are fetched and their fingerprints checked.
3. The shards are recombined into the scrambled piece, the tamper seal is checked, and the piece is unscrambled.
4. The pieces are joined back into the original file.

**Keeping itself healthy (self-healing)**

A background worker keeps checking the shards. If one is missing or damaged, it rebuilds that shard from any 4 others and stores it again, so failures never pile up over time.

## 8. Features and benefits

Each feature maps to a plain benefit for the user.

| Feature | Benefit to the user |
| --- | --- |
| Client-side encryption | No cloud provider, and no attacker who breaches one, can read your files |
| Tamper seal on every piece | Altered, reordered or truncated data is always detected, never silently returned |
| Erasure coding RS(4,2) | Survive the loss of any one cloud at half the cost of three copies |
| At most 2 shards per cloud | No single cloud is a point of failure |
| Self-healing | Missing or damaged shards are rebuilt automatically |
| Pluggable providers | Add, remove or swap clouds without changing how you use the system: no lock-in |
| Works with free tiers and self-hosted storage | Low or zero running cost for small users |
| Residency-aware placement (extension) | Sensitive data stays recoverable within India, supporting DPDP compliance |
| Health-driven migration (extension) | Data moves away from failing clouds before an outage hits |
| Dashboard (optional) | See where shards live and how healthy each cloud is |

## 9. Walkthrough: one file, one failure, full recovery

A college office saves a 12 MB file of exam results; a cloud fails; the file is still fully available and the system repairs itself.

1. **Saving.** The office uploads `results.pdf` (12 MB) and marks it *sensitive*. The system cuts it into 3 pieces: a, b and c.
2. **Scrambling.** A new file key scrambles each piece; each gets a tamper seal and a position label. The file key is locked with the master key and saved.
3. **Splitting.** Each scrambled piece becomes 6 shards: a1–a6, b1–b6, c1–c6 (18 in total).
4. **Spreading.** Cloud A gets a1, a2, b1, b2, c1, c2; Cloud B gets a3, a4, b3, b4, c3, c4; Cloud C gets a5, a6, b5, b6, c5, c6. For a sensitive file, at least 4 shards of each piece are on Indian clouds.
5. **Failure.** Next week, Cloud A has an outage and all 6 of its shards become unreachable.
6. **Still available.** A teacher downloads the file. For each piece the system uses the 4 shards on Clouds B and C, rebuilds and unscrambles it. The teacher notices nothing.
7. **Tampering caught.** Suppose one shard on Cloud B had also been silently altered. Its fingerprint no longer matches, so it is rejected; with Cloud A down this piece would be short of shards, so the system reports a clear error instead of returning a corrupted file.
8. **Self-repair.** When Cloud A returns, or if the office replaces it with another cloud, the background worker rebuilds the missing shards from the survivors and stores them again. Every piece is back to 6 of 6.

At no point could Cloud A, B or C read the results: each only ever held scrambled fragments.

## 10. Scope and limitations

The project delivers a working, measured core; the residency and health-based placement are attempted as an extension, and several production concerns are deliberately left out.

**In scope (core)**

- Upload, download, list and delete through the gateway
- Encryption with per-file keys and a master key
- RS(4,2) erasure coding across three providers
- Surviving the loss of any one provider
- Self-healing of missing or damaged shards
- Measured comparison with full replication

**Extension (attempted after the core works)**

- Residency-aware placement for sensitive files
- Health scoring of clouds and moving shards off failing ones
- A visual dashboard

**Out of scope**

- Acting as a drop-in replacement for Amazon S3
- Mounting the storage as a drive on a computer
- Sharing files between many users with permissions
- Enterprise key management hardware (HSM / KMS)

**Limitations**

| Limitation | Effect |
| --- | --- |
| Two clouds failing at the same time | Affected files cannot be read until one returns (RS(4,3) would tolerate this at higher cost) |
| Metadata database is critical | Losing it loses the map of shards; mitigated by an encrypted backup spread across the clouds |
| Master password is critical | If lost, files cannot be decrypted; it must be backed up safely |
| Extra processing | Encryption and coding add some time to uploads and downloads compared with a single cloud |
| Regions in the demo are simulated | "India" and "abroad" are labels on test storage, not legally certified locations |

## 11. Expected outcomes

Success means a working system that demonstrably survives a cloud failure, plus measured numbers showing what that protection costs.

**Outcomes**

- A working gateway that stores files across three clouds with encryption and erasure coding.
- A live demonstration: one cloud is switched off and files still download; damaged shards are detected and repaired.
- Evidence that no provider holds readable data.
- A report with graphs comparing the approach against keeping full copies.

**How success is measured**

| Question | Measure | Expected result |
| --- | --- | --- |
| Does it survive a cloud failure? | Download success rate with one cloud down | All files still download |
| How much does protection cost? | Storage used vs original data | 1.5×, against 3× for three copies |
| How fast is it? | Upload and download time for files of 1 MB to 500 MB | Measured; slower than a single cloud due to encryption and coding |
| Does it heal itself? | Time to rebuild missing shards after a cloud returns | Measured; all pieces back to 6 of 6 |
| Is tampering caught? | Altered, reordered or dropped pieces | Always rejected, never returned as valid data |
| (Extension) Does residency hold? | Download sensitive files with all foreign clouds blocked | Still rebuildable from Indian clouds |
| (Extension) Does adaptive placement help? | Download time when one cloud is slowed | Faster than fixed placement |

## 12. Future scope

The same foundation can grow into a production-grade storage service.

- **More providers and flexible coding:** support 5–10 clouds and choose the backup level per file (for example RS(6,3) for critical records).
- **S3-compatible front end:** let existing tools and apps use the gateway as if it were Amazon S3.
- **Multi-user sharing:** share encrypted files between users with per-user keys and access control.
- **Smarter placement:** use access patterns and prices to place frequently used data on faster clouds and archives on cheaper ones.
- **Provider auditing:** periodically challenge each cloud to prove it still holds its shards, without downloading them.
- **Certified residency:** use providers with guaranteed Indian data-centre regions to support real DPDP compliance.
- **Hardware key protection:** store the master key in a hardware security module or cloud key-management service.

## 13. Conclusion

SovereignStripe shows that three ordinary clouds, combined carefully, can be more reliable and more private than any one of them alone.

By scrambling every file before it leaves, splitting each piece into six shards with two backups, and never letting a cloud hold more than two, the system survives the complete loss of any provider at half the storage cost of keeping three copies, while no provider ever holds readable data. Self-healing keeps it at full strength over time. The core rests on well-established techniques (AES-256-GCM, Argon2id, Reed–Solomon coding, and the multi-cloud ideas of RACS, DepSky and Storj); the project's contribution is a working, measured implementation over everyday clouds, with residency-aware and health-driven placement explored as an extension relevant to India's data-protection law.
