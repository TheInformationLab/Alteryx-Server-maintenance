"""
MongoDB collection extraction core — memory and crash-safety invariants.

MEMORY INVARIANT:
  Peak additional RSS during extraction is O(batch_size × avg_doc_size), independent of the
  total number of documents in the collection. This is maintained by:
    1. Using a server-side cursor with bounded batch_size (MongoDB driver fetches in batches).
    2. Accumulating at most batch_size Python dicts in the 'rows' list before flushing.
    3. Each flush creates exactly ONE Arrow RecordBatch, writes ONE Parquet row group, then
       is discarded — nothing accumulates in Python memory across batches.
    4. The ParquetWriter streams compressed pages to disk; it does not buffer the full output.
  Any change that materialises the full result set (e.g. list(cursor), cursor.to_list()) is a
  regression of this invariant and must not be made.

CRASH SAFETY (write → upload → commit ordering):
  1. Parquet file is written to a local spool directory.
  2. File is uploaded to S3 (S3 PUT is atomic — no partial objects are visible on failure).
  3. ONLY after confirmed upload: watermark is committed to SQLite in one transaction.
  If the process is killed between steps 2 and 3, the file exists in S3 with the old watermark.
  On next run, the increment is re-extracted to a new S3 key. Downstream MERGE on _id dedupes.
  This is the at-least-once delivery contract. Duplicates are expected and handled downstream.
"""

from __future__ import annotations

from pymongo.collection import Collection
from loguru import logger

from ..config import CollectionConfig, MongoConfig
from ..landing import RowContext, VariantJsonLanding
from ..runner_types import RunContext, UnitResult
from ..s3 import S3Uploader, UploadError, mongo_key
from ..sinks import ParquetVariantSink
from ..spool import spool_path
from ..state import StateStore
from .watermark import make_strategy, validate_watermark_kind


def extract_collection(
    coll: Collection,
    cfg: CollectionConfig,
    state: StateStore,
    uploader: S3Uploader,
    run_ctx: RunContext,
    db_name: str,
) -> UnitResult:
    """Extract one MongoDB collection to a Parquet file in S3, advancing its watermark.

    See the module docstring for the memory and crash-safety invariants this function
    upholds. The step ordering below (write → close → upload → commit → cleanup) is
    load-bearing and must not be reordered.
    """
    namespace = f"mongo:{db_name}.{cfg.name}"
    strategy = make_strategy(cfg)
    logger.info("Extracting collection: {} (mode={})", namespace, cfg.mode)

    prev = state.get_watermark(namespace)
    if prev is not None:
        logger.debug("Existing watermark: kind={} value={}", prev.kind, prev.value)
        validate_watermark_kind(prev, strategy)  # raises ConfigError on mismatch
    else:
        logger.debug("No existing watermark for {}; first run or full_refresh", namespace)

    filter_doc = strategy.build_filter(prev.value if prev else None)
    sort = strategy.sort_spec()
    logger.debug("Query filter={} sort={}", filter_doc, sort)

    cursor = coll.find(
        filter_doc,
        sort=sort,
        batch_size=cfg.batch_size,
        no_cursor_timeout=False,  # cursors time out after 10 min inactivity — acceptable for scheduled runs
    )

    spool_file = spool_path(run_ctx.spool_dir, cfg.name.replace(".", "_"), "parquet")
    landing = VariantJsonLanding(strategy)
    sink = ParquetVariantSink(spool_file, landing)
    ctx = RowContext(extracted_at=run_ctx.run_dt)

    # The Mongo cursor is closed explicitly in finally: on any error (or an early
    # empty/return path) we must not leak the server-side cursor.
    batch_count = 0
    try:
        rows: list[dict] = []
        for doc in cursor:
            strategy.observe(doc)  # MUST be called before append — correctness invariant
            rows.append(landing.doc_to_row(doc, ctx))
            if len(rows) >= cfg.batch_size:
                sink.write_rows(rows)
                batch_count += 1
                logger.debug("Flushed batch {} ({} rows)", batch_count, len(rows))
                rows = []  # CRITICAL: clear immediately — this is the memory invariant

        if rows:  # flush remaining partial batch
            sink.write_rows(rows)
            batch_count += 1
            logger.debug("Flushed final batch {} ({} rows)", batch_count, len(rows))
    except Exception:
        sink.abort()
        raise
    finally:
        cursor.close()

    if sink.rows == 0:
        logger.info("Collection {}: no new documents (watermark unchanged)", cfg.name)
        sink.abort()
        return UnitResult(unit=cfg.name, status="empty")

    # CRASH SAFETY ORDERING — do NOT change the ordering of these steps:
    result = sink.close()  # step 1: finalise parquet footer on disk
    logger.debug("Spool finalised: {} rows in {} batch(es)", result.rows, batch_count)

    if run_ctx.dry_run:
        sink.path.unlink(missing_ok=True)
        logger.info("dry_run: would upload {} ({} rows)", cfg.name, result.rows)
        return UnitResult(unit=cfg.name, status="ok", docs=result.rows, bytes_uploaded=0)

    try:
        key = mongo_key(uploader._prefix, db_name, cfg.name, run_ctx.run_dt)
        upload_result = uploader.upload(spool_file, key)  # step 2: upload to S3 (raises UploadError)
    except UploadError as e:
        sink.abort()
        logger.error("S3 upload failed for {}: {}", cfg.name, e)
        return UnitResult(unit=cfg.name, status="failed", docs=result.rows, error=str(e))

    # ONLY advance watermark after confirmed upload:
    new_wm = strategy.new_watermark()
    if new_wm is not None:
        logger.info("Advancing watermark for {}: {}", namespace, new_wm)
        state.set_watermark(namespace, strategy.kind, new_wm, run_ctx.run_id)  # step 3: commit state
    else:
        logger.debug("No watermark to advance for {} (full_refresh or no docs)", cfg.name)

    spool_file.unlink(missing_ok=True)  # step 4: clean up spool (safe — state already committed)
    logger.info(
        "Collection {} done: docs={} bytes_uploaded={}",
        cfg.name, result.rows, upload_result.bytes_uploaded,
    )
    return UnitResult(
        unit=cfg.name,
        status="ok",
        docs=result.rows,
        bytes_uploaded=upload_result.bytes_uploaded,
    )


def extract_all(
    mongo_cfg: MongoConfig,
    collections: list[CollectionConfig],
    state: StateStore,
    uploader: S3Uploader,
    run_ctx: RunContext,
) -> list[UnitResult]:
    """Connect to Mongo and extract all configured collections.

    Returns one UnitResult per collection. Failures are isolated per collection.
    """
    from .connect import make_client, ping

    try:
        client = make_client(mongo_cfg)
        if not ping(client):
            logger.error("MongoDB ping failed for database '{}'", mongo_cfg.database)
            return [
                UnitResult(unit=cfg.name, status="failed", error="MongoDB ping failed")
                for cfg in collections
            ]
        db = client[mongo_cfg.database]
        logger.debug("Connected to MongoDB database: {}", mongo_cfg.database)
    except Exception as e:
        logger.error("MongoDB connection error: {}", e)
        return [
            UnitResult(unit=cfg.name, status="failed", error=f"MongoDB connect error: {e}")
            for cfg in collections
        ]

    results: list[UnitResult] = []
    try:
        for cfg in collections:
            # Validate: refuse .chunks collections unless allow_gridfs_chunks
            if cfg.name.endswith(".chunks") and not cfg.allow_gridfs_chunks:
                logger.warning(
                    "Skipping GridFS chunks collection '{}' (set allow_gridfs_chunks=true to enable)",
                    cfg.name,
                )
                results.append(
                    UnitResult(
                        unit=cfg.name,
                        status="failed",
                        error="GridFS .chunks collection skipped (set allow_gridfs_chunks=true to enable)",
                    )
                )
                continue
            try:
                result = extract_collection(
                    db[cfg.name], cfg, state, uploader, run_ctx, mongo_cfg.database
                )
                results.append(result)
            except Exception as e:
                logger.exception("Unexpected error extracting {}", cfg.name)
                results.append(UnitResult(unit=cfg.name, status="failed", error=str(e)))
    finally:
        client.close()
        logger.debug("MongoDB client closed")

    return results
