-- ============================================================================
-- mongo-sync-agent Snowflake landing layer
-- 03_tables.sql -- raw landing tables that Snowpipe copies straight into.
--
-- These are deliberately "dumb": one VARIANT payload column plus the small
-- set of typed metadata columns the agent attaches to every row (see
-- VariantJsonLanding in src/mongo_sync_agent/landing.py). No typing,
-- deduplication or flattening happens here -- that is the job of a downstream
-- MERGE/view layer (see 05_merge_example.sql). Keeping the raw tables append-
-- only and untyped means a bad or delayed schema change on the Mongo side can
-- never break ingestion.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- MongoDB collections
--
-- Create one RAW_MONGO_<COLLECTION_NAME> table per [[mongo.collections]]
-- entry in the agent's config, using AS_QUEUE and AS_JOBS below as templates.
-- All such tables must share exactly this schema, since 04_pipes.sql's
-- COPY INTO statements assume this shape.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS RAW_MONGO_AS_QUEUE (
    payload         VARIANT,                                       -- full document as Extended JSON
    _id             STRING,                                        -- doc _id (hex ObjectId or plain string)
    _watermark      STRING,                                        -- canonical watermark value for this doc
    _extracted_at   TIMESTAMP_TZ,                                  -- when the agent read this doc from Mongo
    _loaded_at      TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()        -- when Snowpipe loaded this row
);

CREATE TABLE IF NOT EXISTS RAW_MONGO_AS_JOBS (
    payload         VARIANT,
    _id             STRING,
    _watermark      STRING,
    _extracted_at   TIMESTAMP_TZ,
    _loaded_at      TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()
);

-- Template for any further collections -- copy/paste and rename:
-- CREATE TABLE IF NOT EXISTS RAW_MONGO_<COLLECTION_NAME> (
--     payload         VARIANT,
--     _id             STRING,
--     _watermark      STRING,
--     _extracted_at   TIMESTAMP_TZ,
--     _loaded_at      TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()
-- );

-- ----------------------------------------------------------------------------
-- Shipped log lines (all sources land in one table; discriminate on SOURCE)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS RAW_LOGS (
    line            STRING,                                        -- raw log line, verbatim
    source          STRING,                                        -- log source name, e.g. "gallery" / "service"
    file            STRING,                                        -- name of the file the line was tailed from
    file_offset     INTEGER,                                       -- byte offset in the source file after this line
    shipped_at      TIMESTAMP_TZ,                                  -- when the agent shipped this line
    _loaded_at      TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()
);

-- ----------------------------------------------------------------------------
-- Host metrics (CPU, memory, disk usage/IO)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS RAW_HOSTMETRICS (
    payload         VARIANT,                                       -- full metric record (metric/value/... fields)
    ts              TIMESTAMP_TZ,                                  -- sample timestamp, pulled out for pruning
    _loaded_at      TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()
);
