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

From the repository root (`mongo-sync-agent/`), ideally inside a dedicated
virtual environment:

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
as a long-lived service. On Windows, use the provided registration script:

```powershell
.\deploy\Register-MongoSyncTask.ps1 `
    -PythonPath "C:\path\to\venv\Scripts\pythonw.exe" `
    -ConfigPath "C:\ProgramData\mongo-sync-agent\config\config.toml" `
    -WorkingDir "C:\ProgramData\mongo-sync-agent"
```

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

## 8. Snowflake setup

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

## 9. Running manually / testing

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

## 10. Known limitations

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

## 11. Running the test suite

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
