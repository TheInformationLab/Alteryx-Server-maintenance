-- ============================================================================
-- mongo-sync-agent Snowflake landing layer
-- 02_file_formats.sql -- file formats matching the two output encodings the
-- agent writes to S3:
--   * mongo/**            -> Snappy-compressed Parquet (VariantJsonLanding)
--   * logs/**, hostmetrics/** -> gzip-compressed NDJSON (one JSON object per line)
-- ============================================================================

-- Parquet landing format for MongoDB collection extracts.
CREATE FILE FORMAT IF NOT EXISTS MSA_PARQUET
    TYPE = PARQUET
    SNAPPY_COMPRESSION = TRUE
    COMMENT = 'Parquet files produced by mongo-sync-agent for MongoDB collection extracts (mongo/<db>/<collection>/...).';

-- Gzip-compressed newline-delimited JSON for shipped log lines and host
-- metrics. STRIP_OUTER_ARRAY is FALSE because each file is one JSON object
-- per line, not a single JSON array; STRIP_NULL_VALUES is FALSE so that
-- explicit nulls (e.g. a metric with no value) are preserved in the VARIANT
-- rather than silently dropped.
CREATE FILE FORMAT IF NOT EXISTS MSA_NDJSON_GZ
    TYPE = JSON
    COMPRESSION = GZIP
    STRIP_OUTER_ARRAY = FALSE
    STRIP_NULL_VALUES = FALSE
    COMMENT = 'Gzip NDJSON files produced by mongo-sync-agent for shipped logs (logs/<source>/...) and host metrics (hostmetrics/...).';
