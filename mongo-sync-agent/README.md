# mongo-sync-agent

A lightweight, poll-based agent that incrementally replicates data out of an
Alteryx Server's MongoDB (queue/job metadata), its Gallery/Service log files,
and basic host metrics, landing everything in Amazon S3 for ingestion into
Snowflake via Snowpipe.

## 1. Overview

Alteryx Server stores operational state (job queue, job history, schedules,
worker registration, etc.) in a MongoDB database, and writes a variety of
CSV/plain-text log files to disk. None of this is directly queryable from a
data warehouse. **mongo-sync-agent (`msa`)** closes that gap: it runs
alongside (or near) an Alteryx Server/Controller node, periodically reads
whatever has changed since its last run, and drops the results as Parquet
(for Mongo data) or line-delimited files (for logs) into an S3 "landing
zone", from where Snowpipe picks them up automatically.

**Who this is for:** Alteryx Server administrators and platform/data teams
who want visibility into Server usage, job throughput, queue depth, and
operational health in Snowflake or a BI tool sitting on top of it, without
touching Alteryx's supported APIs or the AlteryxService itself.

**Architecture, in brief:**

```
 Alteryx Server host                                   AWS
┌───────────────────────────┐                    ┌────────────────────┐
│  MongoDB (embedded or      │   poll (read-only) │                    │
│  self-managed standalone)  │◄────────────────┐  │                    │
│                             │                 │  │                    │
│  Gallery / Service logs     │──tail───────────┤  │   S3 landing zone   │
│  (CSV / plain text)         │                 ├─►│   (Parquet + NDJSON)│
│                             │                 │  │                    │
│  Host metrics (disk, CPU,   │──sample─────────┘  │                    │
│  memory via psutil)         │                    └─────────┬──────────┘
└───────────────────────────┘                              │ Snowpipe
        mongo-sync-agent (msa)                                 │ (SQS-notified)
        runs on a schedule,                                    ▼
        no inbound connections                        ┌────────────────┐
        required — it only opens                       │   Snowflake     │
        outbound connections to                        │   raw tables    │
        Mongo, the filesystem, and S3                  └────────────────┘
```

Key design points:

- **Poll-based, not a change stream.** The agent runs, does one pass, and
  exits (or sleeps, depending on how it's invoked). There is no long-lived
  connection or `$changeStream` cursor, no daemon listening on a port, and no
  inbound network exposure whatsoever — this makes it trivial to reason
  about from a security perspective.
- **S3 is the landing zone, Snowpipe does the ingestion.** The agent's only
  job is to get clean, deduplicated-as-far-as-possible files into S3 with a
  predictable key layout. Snowpipe (triggered by S3 event notifications via
  SQS) handles loading into Snowflake; the agent has no Snowflake
  connectivity or credentials at all.
- **State is local and disposable.** A small SQLite database on the host
  tracks per-collection watermarks, log file offsets, and run history, so a
  restart resumes where it left off rather than re-scanning everything.

## 2. Prerequisites

- **Python 3.11+** on the host that will run the agent (this can be the
  Alteryx Server/Controller box itself, or any host with network access to
  the Mongo instance and to the log files it needs to tail).
- Python packages (installed automatically as dependencies, see below):
  - `pymongo` — MongoDB driver
  - `pyarrow` — Parquet writing
  - `boto3` — S3 upload
  - `psutil` — host metrics (disk, CPU, memory)
- **AWS credentials** available to the process (environment variables,
  shared credentials file, instance profile, etc.) with `s3:PutObject`
  permission on the target bucket/prefix. The agent uses boto3's standard
  credential resolution chain — there is no separate credentials
  configuration inside `msa` itself.
- **Network/filesystem access to MongoDB** — either the embedded Alteryx
  Server MongoDB (default `localhost:27018`) or a self-managed standalone
  `mongod`/replica set/Atlas cluster, plus a read-only Mongo user with
  access to the databases/collections you intend to sync.

## 3. Installation

**Clients / production Alteryx Server hosts (no Python required):** download
the latest release zip from the project's GitHub **Releases** page, unzip it,
and use the self-contained `msa.exe` — see
[§13. Releasing & distribution](#13-releasing--distribution) and
[`RELEASING.md`](RELEASING.md) for the full install steps. Nothing needs to be
installed on the host.

**From source (development / testing):** from `mongo-sync-agent/`, ideally
inside a dedicated virtual environment:

```
pip install -e .
pip install -e .[dev]   # additionally installs pytest, for running the test suite
```

This installs the `msa` console script (see `[project.scripts]` in
`pyproject.toml`) as well as the `mongo_sync_agent` package, so it can be
invoked either as `msa ...` or as `python -m mongo_sync_agent ...`.

## 4. Configuration

The agent is configured entirely via a single TOML file, passed with
`--config`/`-c`. Two fully-commented example configurations are provided:

- [`config/config.example.standalone.toml`](config/config.example.standalone.toml)
  — for a self-managed, standalone `mongod` (see §5a).
- [`config/config.example.alteryx-embedded.toml`](config/config.example.alteryx-embedded.toml)
  — for Alteryx Server's embedded MongoDB (see §5b).

The two files are identical apart from the `[mongo]` connection section;
copy whichever matches your deployment to `config/config.toml` (or any path
you like) and edit it in place. The main sections are:

| Section | Purpose |
|---|---|
| `[agent]` | Paths for the local SQLite state DB, the Parquet staging/spool directory, and the agent's own logs. |
| `[mongo]` | Connection details plus one `[[mongo.collections]]` block per collection to sync (mode, watermark field, batch size, overlap window). |
| `[s3]` | Destination bucket, region, and key prefix. |
| `[logs]` | Whether log shipping is enabled, retention (`gc_days`), and one `[[logs.sources]]` block per log glob/encoding to tail. |
| `[hostmetrics]` | Whether host metric sampling is enabled, and which disks to report on. |

Configuration errors (missing required fields, an unrecognised `mode`, a
`.chunks` collection without `allow_gridfs_chunks = true`, etc.) are raised
as `ConfigError` at start-up rather than failing partway through a run.

## 5. The two Mongo targets

`mongo-sync-agent` supports exactly two ways of reaching Alteryx's queue/job
data, corresponding to how Alteryx Server itself was set up.

### a. Self-managed standalone Mongo

If you pointed AlteryxService at your own `mongod` (or a replica set, or
Atlas), this is the straightforward case: supply `host`, `port`, `username`,
`password`, and `auth_source` under `[mongo]` (or a single `uri =
"mongodb://..."` connection string instead — `uri` always takes priority
over the discrete fields if both are present). Enable `tls = true` and
supply `tls_ca_file` if your deployment requires encrypted connections. See
`config/config.example.standalone.toml` for a fully worked example.

### b. Embedded Alteryx Server MongoDB (important note)

Most Alteryx Server installs use the MongoDB instance **embedded with the
AlteryxService** rather than an external one. Key facts:

- It normally listens on **`localhost:27018`** by default (not the standard
  `27017`), and is only reachable from the Controller host itself.
- **Retrieving the credentials.** There are two ways to obtain the
  password, both performed on the Controller host:
  1. **(Preferred)** From an elevated command prompt in the Alteryx Service
     install `bin` directory, run:
     ```
     AlteryxService.exe getemongopassword
     ```
     This prints the plaintext password to use directly in your
     configuration.
  2. **(Manual fallback)** The same credential is stored in
     `RuntimeSettings.xml`, under `Controller → Persistence → Password`. The
     value there is AES-encrypted (and base64-encoded) — it is not meant to
     be decrypted by hand. Use the `AlteryxService.exe getemongopassword`
     command above instead of attempting to reverse the encryption
     yourself; it returns the plaintext value that the encrypted field
     represents. The corresponding username is normally `controller` and
     can be confirmed against `Controller → Persistence → Username` in the
     same file if you need to double-check it.

  A worked example, including where to plug the resulting credential into
  the connection URI, is in
  `config/config.example.alteryx-embedded.toml`.

> **Polling the embedded MongoDB is a read-only operation and poses minimal
> risk, but it is NOT part of Alteryx's formally supported configuration.
> Inform your Alteryx account team if using this in a production
> environment.**

## 6. The two-class watermarking model

Every collection you sync is configured with a `mode`, which determines how
the agent tracks "what's new since last time". This is the load-bearing
correctness logic of the agent (see
`src/mongo_sync_agent/mongo/watermark.py` for the full implementation and
rationale in code comments) and boils down to two real strategies plus one
escape hatch:

- **`append_only`** — for collections where documents are only ever
  inserted, never updated (e.g. a queue/history log such as `AS_Queue`). The
  watermark is the highest `_id` (a MongoDB `ObjectId`) seen so far, and the
  next run filters on `_id > watermark`.

  **Why the overlap window is mandatory:** `ObjectId`s are generated
  client-side, not by the MongoDB server, so two `ObjectId`s minted within
  the same second are *not* guaranteed to be ordered the same way their
  documents were actually inserted into the collection. Without
  compensating for this, a document that lands "behind" the current
  watermark's timestamp second could be permanently skipped by a strict
  `$gt` filter. Every `append_only` collection therefore re-scans an
  `overlap_seconds` window behind the previous watermark on every run.
  Re-reading a handful of already-seen documents is harmless (see
  deduplication note below); silently dropping one is not.

- **`mutable`** — for collections whose documents can be updated after
  insertion (e.g. `AS_Jobs`, where a job's status/timestamps change as it
  progresses). This mode requires a `watermark_field` — a field that is
  guaranteed to change on every update (e.g. `dtModified`) — and the
  watermark becomes the maximum value seen for that field.

  **Why there is no sort:** unlike `append_only`, the extractor deliberately
  does **not** ask MongoDB to `sort()` the mutable-mode query. Sorting on an
  arbitrary application field would either require an index on that source
  field (which cannot be assumed to exist) or risk hitting MongoDB's 100 MB
  in-memory sort limit on a modestly-resourced Controller box. Instead, the
  agent streams the (unsorted) cursor and tracks the running maximum of
  `watermark_field` itself in application code, which needs no index and no
  server-side memory budget.

  For small reference/configuration-style collections that have **no**
  timestamp-like field to watermark on at all, use **`full_refresh`**
  instead: the entire collection is re-read and re-uploaded on every run.
  This only makes sense for genuinely small collections — it does not scale
  to anything queue- or history-sized.

- **At-least-once delivery, by design.** Both the mandatory overlap window
  (`append_only`) and the inclusive `$gte` boundary (`mutable`) mean the
  agent will, from time to time, re-emit documents it has already sent. This
  is a deliberate trade-off: it is far safer to occasionally re-send a row
  than to silently miss one. The corresponding Snowflake-side artefact
  (`sql/05_merge_example.sql`, see below) is expected to `MERGE` on a stable
  key (`_id` for `append_only`, `_id` again for `mutable`) so that
  duplicate deliveries are idempotent and never produce duplicate rows in
  the warehouse.

## 7. Scheduling — Windows Scheduled Task

The agent is designed to be invoked on a recurring schedule rather than run
as a long-lived service. On Windows, use the provided registration script,
which supports two modes.

**Client / production (packaged `msa.exe`, no Python on the host):**

```powershell
.\deploy\Register-MongoSyncTask.ps1 `
    -ExePath "C:\ProgramData\mongo-sync-agent\msa.exe" `
    -ConfigPath "C:\ProgramData\mongo-sync-agent\config\config.toml" `
    -WorkingDir "C:\ProgramData\mongo-sync-agent"
```

**Development / source install (via a venv Python):**

```powershell
.\deploy\Register-MongoSyncTask.ps1 `
    -PythonPath "C:\path\to\venv\Scripts\pythonw.exe" `
    -ConfigPath "C:\ProgramData\mongo-sync-agent\config\config.toml" `
    -WorkingDir "C:\ProgramData\mongo-sync-agent"
```

`-ExePath` takes precedence over `-PythonPath` when both are supplied.

Run it from an elevated PowerShell prompt (it registers the task to run with
`RunLevel Highest`, needed to read some Alteryx/ProgramData paths). Add
`-WhatIf` first to preview what will be registered without making any
changes:

```powershell
.\deploy\Register-MongoSyncTask.ps1 -WhatIf
```

By default the script registers a task named `MongoSyncAgent` that repeats
every 30 minutes, indefinitely, running as the local `SYSTEM` account. Pass
`-TaskUser`/`-TaskPassword` if you need it to run as a dedicated service
account instead. See the header comment in the script itself for the full
parameter list and further examples.

**Recommended interval:** every **30–60 minutes**. This is frequent enough
to keep the warehouse close to real-time for monitoring/alerting purposes,
while keeping the overlap-window re-reads and S3 PUT volume modest. There is
no benefit to running much more often than this given the agent's
poll-based, at-least-once design.

## 8. Multi-controller / high-availability deployments

Some Alteryx Server estates run more than one Controller node in an
**active/passive failover** arrangement: at any moment exactly one node is the
active Controller (recording jobs and writing Gallery/Service logs), the
other(s) stand by, and **all nodes share a single MongoDB instance**. The
agent supports this; the key facts to deploy it correctly are below.

### Every row is stamped with its host

Every record the agent emits — Mongo documents (`_host`), shipped log lines
(`host`), and host metrics (`host`) — carries the identifier of the machine
that produced it (from `[agent] host_id`, else the OS machine name; see §4).
This is what lets you tell, downstream in Snowflake, which node was active when
a given row was produced, and to attribute host metrics to the right box.

Choose your `host_id` convention deliberately:

- **Per-node identity** (recommended): leave `host_id` unset, or set a distinct
  value per node (`controller-a`, `controller-b`). You can see exactly which
  physical node extracted each row and which was active over time.
- **Single cluster identity**: set the *same* `host_id` on every node if you
  would rather treat the pair as one logical source and don't care which
  physical node was active.

### Where to run the agent, per module

- **Logs and host metrics are host-local.** Run the agent on **every** node so
  each ships its own files. The active node produces the Gallery logs; a
  standby mostly produces its own host metrics and Service logs. This is
  always safe — no two nodes share these files.
- **Mongo is a shared source.** Because all nodes point at the *same* MongoDB,
  you do **not** want two agents extracting the same collections concurrently —
  that doubles S3/Snowpipe volume (correctness still holds via the
  at-least-once + `MERGE` design in §6, but the work is wasted). In a true
  active/passive setup where only the active node runs the agent, this never
  arises. If the agent runs on standby nodes too, restrict them to the
  host-local modules and let only the active node do Mongo:

  ```
  # On a standby node (no Mongo extraction):
  msa --config config.toml --only logs hostmetrics
  ```

### Watermark state on failover

The agent's watermark/offset state is a **local SQLite DB** (`[agent]
state_db`). That has one important consequence when the active role moves:

- **If you keep per-node state** (the default — each node has its own
  `state_db` on local disk), a node that becomes active resumes from *its own*
  last watermark. Thanks to the mandatory overlap window and inclusive
  boundaries (§6), this never loses data — but if that node's state is stale
  (it hasn't been active for a while) it will re-read everything since its last
  run, and the `MERGE` layer dedupes the overlap. Safe, occasionally a larger
  re-scan. Setting a sensible `initial_watermark` on a freshly provisioned node
  avoids a full historical re-scan on its first activation.

- **If you want seamless failover**, point `state_db` (and `spool_dir`) at the
  **shared/clustered storage that follows the active role**, so whichever node
  is active reads and writes the same watermarks and picks up exactly where the
  other left off. This is safe *specifically because only one node is active at
  a time* — SQLite must never be written by two nodes at once, and the
  active/passive model guarantees a single writer. Do **not** put the state DB
  on a plain SMB share written by multiple concurrently-running agents.

For the common active/passive case the simplest correct recipe is: run the
agent on the active node (failover brings up the agent on whichever node is
active), keep Mongo extraction to that single active agent, and either accept
the bounded re-scan on failover or share the state DB on the failover volume.

## 9. Snowflake setup

Snowflake-side DDL/DML artefacts live under `sql/`. Apply them in order:

1. `sql/01_stage.sql` — external stage pointing at the S3 landing zone.
2. `sql/02_file_formats.sql` — Parquet and NDJSON/CSV file format objects.
3. `sql/03_tables.sql` — raw landing tables (one per Mongo collection / log
   source / metrics stream).
4. `sql/04_pipes.sql` — Snowpipe definitions that auto-ingest new files as
   they land in S3.
5. `sql/05_merge_example.sql` — an example `MERGE` statement showing how to
   deduplicate the at-least-once delivered rows into a clean, keyed table
   (see §6 above for why this is necessary).

**Snowpipe notification note:** Snowpipe's auto-ingest mode relies on an S3
event notification delivered via an SQS queue that Snowflake creates for
you. After creating each pipe (step 4), retrieve its notification channel
ARN with `SHOW PIPES` / `DESC PIPE`, and configure the S3 bucket's event
notification to publish `s3:ObjectCreated:*` events for the relevant prefix
to that SQS queue (this is a one-time manual step in the S3 console or via
`aws s3api put-bucket-notification-configuration`) — Snowflake cannot
configure this on your bucket for you.

## 10. Running manually / testing

Run a dry run against the standalone example config (processes data but
does not upload to S3 or advance watermarks):

```
msa --config config/config.example.standalone.toml --dry-run
```

Run only the Mongo module, and only a single collection, against a real
config file:

```
msa --config config.toml --only mongo --collection AS_Queue
```

Other useful flags (see `msa --help`):

- `--only mongo logs hostmetrics` — restrict a run to one or more modules
  (default: all enabled modules).
- `--collection NAME` / `-C NAME` — restrict the Mongo module to a single
  collection.
- `--dry-run` — process everything but skip the S3 upload and watermark
  advance, useful for validating configuration changes safely.

## 11. Known limitations

Please read this section before treating the warehouse copy as anything
more than what it is. Being upfront about these constraints is preferable
to discovering them by surprise:

- **No deletes are captured.** The agent only ever reads and appends; if a
  document is deleted from MongoDB, the corresponding row(s) already landed
  in the warehouse are never removed. Over time the warehouse will
  accumulate rows for entities that no longer exist in Mongo. This makes
  the result well-suited to analytics, trend reporting, and audit/history
  use cases, but it is **not a live mirror** of the source collection.
- **Updates are only captured where a modified-timestamp field exists**
  (i.e. for `mutable`-mode collections with a working `watermark_field`),
  or via `full_refresh` for small collections. A collection that is neither
  `append_only` nor has a reliable modified-timestamp field, and is too
  large to `full_refresh`, cannot have its updates captured at all —
  reconfigure it or accept a stale copy for that field.
- **No cross-collection point-in-time consistency.** Each collection is
  polled independently, on its own schedule/cadence and cursor. Two
  collections extracted in the same agent run are not guaranteed to reflect
  the exact same instant in Mongo, so joins across collections landed in
  Snowflake should not be assumed to be perfectly time-aligned.
- **Encrypted fields land as ciphertext.** Alteryx encrypts certain
  sensitive fields at rest (e.g. DCM credential material, `secureComms`
  payloads). The agent copies these fields verbatim — it has no access to
  the Controller's decryption key/token — so they arrive in the warehouse
  as opaque ciphertext, useful only for presence/existence checks, not for
  content inspection.
- **GridFS binary chunks are excluded by default.** Only GridFS file
  *metadata* (the `.files` collection) is synced by default; the
  corresponding `.chunks` collection (the actual binary payload, potentially
  very large) is skipped unless you explicitly set
  `allow_gridfs_chunks = true` on that collection's config block. Enable
  this at your own risk — it can dramatically increase both S3 storage
  footprint and the agent's in-memory footprint while assembling Parquet
  batches.
- **Gallery CSV `Exception` column may span multiple lines.** Alteryx's
  Gallery log CSVs can contain embedded newlines within the `Exception`
  field for multi-line stack traces. Version 1 of the agent ships these as
  raw, un-reassembled lines rather than attempting CSV-aware multi-line
  parsing at tail time; reconstructing the full multi-line exception text is
  left as a Snowflake-side concern (e.g. window functions over ingestion
  order) rather than solved in the agent itself.
- **The embedded Mongo polling route is read-only and low-risk, but it is
  outside Alteryx's formally supported configuration** (see §5b). Treat it
  accordingly, and flag it to your Alteryx account team if you rely on it in
  production.
- **Host metrics are point-in-time samples, not a continuous PerfMon-style
  feed.** The agent is a one-shot process launched by the Scheduled Task on its
  interval (§7); each run captures a single sample, so `RAW_HOSTMETRICS` has one
  row per metric per tick (every 30–60 min), *not* a per-second series. Two
  further points to understand before querying the disk numbers:
  - **`disk_io` is a cumulative counter, not an interval delta.** psutil's
    `read_bytes`/`write_bytes`/`read_count`/`write_count` are running totals
    **since the last OS boot** (equivalent to PerfMon's raw
    `\PhysicalDisk\Disk Bytes` counter *before* rate conversion), not the I/O
    that occurred during the last interval. To get throughput, difference
    consecutive samples per `host`+`path` in Snowflake, e.g.
    `read_bytes - LAG(read_bytes) OVER (PARTITION BY host, path ORDER BY ts)`
    divided by the actual `ts` gap. **Discard negative deltas** — the counter
    resets to zero on reboot — and divide by the real time gap rather than an
    assumed interval, so a missed run doesn't distort the rate. By contrast
    `cpu_percent_1s` (a rate over a 1-second window) and the `memory`/
    `disk_usage` gauges are meaningful on their own row and need no
    differencing.
  - **On Windows, `disk_io` is attributed by mapping the configured drive
    letter to its backing physical drive(s).** psutil keys its per-disk I/O
    counters by physical drive (`PhysicalDrive0`, …), not by drive letter, so
    the agent resolves each configured letter to its physical disk(s) via the
    `IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS` Win32 control code and sums their
    counters (a volume can span more than one physical disk). If that mapping
    can't be resolved, the record is still emitted with zeroed counters and a
    `warning` key rather than being dropped.

## 12. Running the test suite

Install the `dev` extra first (`pip install -e .[dev]`), then:

```
pytest                                        # unit tests
pytest -m slow                                # includes the memory-invariant empirical test
MSA_TEST_MONGO_URI=mongodb://... pytest tests/integration/   # integration tests against a real mongod
```

The integration suite under `tests/integration/` expects a real, reachable
`mongod` (a local instance or a disposable container is fine) and reads its
connection string from the `MSA_TEST_MONGO_URI` environment variable; it is
skipped automatically when that variable is not set. The `slow` marker
gates a small number of longer-running, empirically-verified tests (e.g.
confirming memory usage stays bounded over a large synthetic extraction)
that are not part of the default fast unit-test run.

## 13. Releasing & distribution

Releases are **tag-driven** and built automatically by GitHub Actions
([`.github/workflows/release.yml`](../.github/workflows/release.yml)): pushing
a `v…` tag builds a self-contained Windows `msa.exe` bundle and publishes it as
a GitHub Release. Beta and release channels share one `master` branch and are
distinguished only by the tag shape:

- `v1.2.0-beta.1` → **pre-release** (for the dev/testing area)
- `v1.2.0` → **release** (client-facing)

The full branching strategy, versioning scheme, and step-by-step process for
cutting a beta or a release is documented in **[`RELEASING.md`](RELEASING.md)**.

## 14. Roadmap / future work

- **Higher-resolution host metrics via a PerfMon collector.** The current host
  metrics are coarse point-in-time samples taken once per scheduled run (§7,
  §11) — fine for trend/capacity reporting, but they leave gaps between ticks
  and give no visibility into short-lived spikes (e.g. a disk saturating for two
  minutes between 30-minute samples). A future enhancement would add a
  continuous, higher-frequency collector — most naturally a Windows Performance
  Monitor (PerfMon) **Data Collector Set** logging counters such as
  `\PhysicalDisk(*)\Disk Bytes/sec`, `\Processor(_Total)\% Processor Time`, and
  `\Memory\Available Bytes` at a per-minute (or finer) cadence — with the agent
  tailing/rolling up those PerfMon logs and shipping them to S3 alongside the
  existing samples. This would fill the gaps between the coarse samples and
  provide already-rate-converted values, removing the need for the
  counter-differencing described in §11. Design points to settle when this is
  picked up: whether to drive PerfMon via a bundled `.xml` collector template
  (`logman`) or read counters directly (e.g. via `typeperf`/PDH), the retention
  and rollup strategy for the finer-grained data, and how it coexists with (or
  supersedes) the current psutil `disk_io`/`cpu`/`memory` records in Snowflake.
