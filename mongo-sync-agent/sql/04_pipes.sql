-- ============================================================================
-- mongo-sync-agent Snowflake landing layer
-- 04_pipes.sql -- Snowpipe auto-ingest pipes, one per S3 prefix the agent
-- writes to, copying straight into the raw tables from 03_tables.sql.
--
-- AUTO_INGEST setup (do this after running this file):
--   1. Run `DESC PIPE <pipe_name>;` (or `SHOW PIPES;`) and note the
--      "notification_channel" column -- this is an SQS queue ARN that
--      Snowflake owns and manages for you; you do not create or manage this
--      queue yourself.
--   2. In the S3 bucket's event notification settings, add a new event
--      notification (event type: "All object create events" /
--      s3:ObjectCreated:*) scoped to the relevant prefix (e.g.
--      "mongo/AlteryxService/AS_QUEUE/"), with the destination set to that
--      SQS queue ARN.
--   3. Repeat per pipe/prefix below -- each pipe needs its own S3 event
--      notification pointed at its own notification_channel, scoped to its
--      own prefix, so that files land in the correct raw table.
--   4. New objects landed under the prefix will then trigger the pipe
--      automatically within roughly a minute; use SELECT
--      SYSTEM$PIPE_STATUS('<pipe_name>') or the copy history to confirm.
--
-- Every COPY below explicitly sets MATCH_BY_COLUMN_NAME = NONE: column
-- mapping is handled entirely by the SELECT list (using $1:<field> to pull
-- fields out of the staged Parquet/JSON), so we do not want Snowflake
-- attempting to auto-match source and target columns by name as well.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- MongoDB collections (Parquet, one pipe per collection prefix)
-- ----------------------------------------------------------------------------

CREATE PIPE IF NOT EXISTS PIPE_MONGO_AS_QUEUE
    AUTO_INGEST = TRUE
AS
COPY INTO RAW_MONGO_AS_QUEUE (payload, _id, _watermark, _extracted_at, _host)
FROM (
    SELECT
        -- payload is landed as a JSON *string* column inside the Parquet file
        -- (see VariantJsonLanding) -- PARSE_JSON turns it into a proper
        -- VARIANT object so downstream payload:"field" access works.
        PARSE_JSON($1:payload::STRING),
        $1:_id::STRING,
        $1:_watermark::STRING,
        $1:_extracted_at::TIMESTAMP_TZ,
        $1:_host::STRING
    FROM @<STAGE_NAME>/mongo/AlteryxService/AS_QUEUE/
)
FILE_FORMAT = (FORMAT_NAME = MSA_PARQUET)
MATCH_BY_COLUMN_NAME = NONE;

CREATE PIPE IF NOT EXISTS PIPE_MONGO_AS_JOBS
    AUTO_INGEST = TRUE
AS
COPY INTO RAW_MONGO_AS_JOBS (payload, _id, _watermark, _extracted_at, _host)
FROM (
    SELECT
        PARSE_JSON($1:payload::STRING),
        $1:_id::STRING,
        $1:_watermark::STRING,
        $1:_extracted_at::TIMESTAMP_TZ,
        $1:_host::STRING
    FROM @<STAGE_NAME>/mongo/AlteryxService/AS_JOBS/
)
FILE_FORMAT = (FORMAT_NAME = MSA_PARQUET)
MATCH_BY_COLUMN_NAME = NONE;

-- Template for any further collections -- copy/paste, rename the pipe, the
-- target table and the stage sub-path:
-- CREATE PIPE IF NOT EXISTS PIPE_MONGO_<COLLECTION_NAME>
--     AUTO_INGEST = TRUE
-- AS
-- COPY INTO RAW_MONGO_<COLLECTION_NAME> (payload, _id, _watermark, _extracted_at, _host)
-- FROM (
--     SELECT
--         PARSE_JSON($1:payload::STRING),
--         $1:_id::STRING,
--         $1:_watermark::STRING,
--         $1:_extracted_at::TIMESTAMP_TZ,
--         $1:_host::STRING
--     FROM @<STAGE_NAME>/mongo/AlteryxService/<COLLECTION_NAME>/
-- )
-- FILE_FORMAT = (FORMAT_NAME = MSA_PARQUET)
-- MATCH_BY_COLUMN_NAME = NONE;

-- ----------------------------------------------------------------------------
-- Shipped log lines (gzip NDJSON, one pipe per log source prefix, same target)
-- ----------------------------------------------------------------------------

CREATE PIPE IF NOT EXISTS PIPE_LOGS_GALLERY
    AUTO_INGEST = TRUE
AS
COPY INTO RAW_LOGS (line, host, source, file, file_offset, shipped_at)
FROM (
    SELECT
        $1:line::STRING,
        $1:host::STRING,
        $1:source::STRING,
        $1:file::STRING,
        $1:file_offset::INTEGER,
        $1:shipped_at::TIMESTAMP_TZ
    FROM @<STAGE_NAME>/logs/gallery/
)
FILE_FORMAT = (FORMAT_NAME = MSA_NDJSON_GZ)
MATCH_BY_COLUMN_NAME = NONE;

CREATE PIPE IF NOT EXISTS PIPE_LOGS_SERVICE
    AUTO_INGEST = TRUE
AS
COPY INTO RAW_LOGS (line, host, source, file, file_offset, shipped_at)
FROM (
    SELECT
        $1:line::STRING,
        $1:host::STRING,
        $1:source::STRING,
        $1:file::STRING,
        $1:file_offset::INTEGER,
        $1:shipped_at::TIMESTAMP_TZ
    FROM @<STAGE_NAME>/logs/service/
)
FILE_FORMAT = (FORMAT_NAME = MSA_NDJSON_GZ)
MATCH_BY_COLUMN_NAME = NONE;

-- ----------------------------------------------------------------------------
-- Host metrics (gzip NDJSON)
-- ----------------------------------------------------------------------------

CREATE PIPE IF NOT EXISTS PIPE_HOSTMETRICS
    AUTO_INGEST = TRUE
AS
COPY INTO RAW_HOSTMETRICS (payload, host, ts)
FROM (
    SELECT
        $1,                     -- keep the whole metric record (metric/value/... vary by type)
        $1:host::STRING,
        $1:ts::TIMESTAMP_TZ
    FROM @<STAGE_NAME>/hostmetrics/
)
FILE_FORMAT = (FORMAT_NAME = MSA_NDJSON_GZ)
MATCH_BY_COLUMN_NAME = NONE;
