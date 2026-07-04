-- ============================================================================
-- mongo-sync-agent Snowflake landing layer
-- 05_merge_example.sql -- idempotent MERGE pattern for "mutable" collections
-- (see the `mode = "mutable"` collections in the agent config, e.g. AS_Jobs).
--
-- Why this is needed: the agent's delivery guarantee is at-least-once, not
-- exactly-once (see the crash-safety notes in mongo/extract.py and
-- logs/ship.py -- if the process is killed after an S3 upload but before the
-- local watermark/offset is committed, the same rows are re-extracted and
-- re-uploaded as a new file on the next run). RAW_MONGO_AS_JOBS is therefore
-- expected to contain duplicate _id values across different files, and this
-- MERGE is how a clean, deduplicated target table is derived from the raw
-- landing table -- run it on a schedule (e.g. a Snowflake TASK) after the
-- pipe has caught up.
-- ============================================================================

-- Stage the raw data with deduplication (latest extraction wins):
CREATE OR REPLACE TEMPORARY TABLE STAGED_AS_JOBS AS
SELECT
    _id,
    payload,
    _watermark,
    _extracted_at,
    ROW_NUMBER() OVER (PARTITION BY _id ORDER BY _extracted_at DESC) AS rn
FROM RAW_MONGO_AS_JOBS;

-- Merge into a clean target table:
-- The target table (e.g. MONGO_AS_JOBS) must exist with appropriate typed columns.
-- To access fields from the VARIANT payload:
--   payload:"dtModified"::TIMESTAMP_TZ        → the last modified date
--   payload:"_id":"$oid"::STRING               → the ObjectId as a hex string
--   payload:"Status"::STRING                  → a string field
--   payload:"SomeInt"::INTEGER                → an integer field
-- Note: "$oid", "$date" etc. are Extended JSON keys emitted by the agent.

MERGE INTO MONGO_AS_JOBS AS target
USING (SELECT * FROM STAGED_AS_JOBS WHERE rn = 1) AS source
ON target._id = source._id
WHEN MATCHED THEN UPDATE SET
    target.payload       = source.payload,
    target._watermark    = source._watermark,
    target._extracted_at = source._extracted_at
WHEN NOT MATCHED THEN INSERT (_id, payload, _watermark, _extracted_at)
    VALUES (source._id, source.payload, source._watermark, source._extracted_at);

-- ----------------------------------------------------------------------------
-- Known limitations of this pattern
-- ----------------------------------------------------------------------------
-- * No deletes are captured. The agent only ever reads and lands documents
--   that still exist in MongoDB at extraction time (via the watermark_field
--   for mutable collections); if a document is deleted from Mongo, this
--   MERGE has no way of knowing and the row simply lingers unchanged in the
--   target table forever. If hard deletes matter downstream, they need to be
--   reconciled separately (e.g. a periodic full-collection scan/anti-join),
--   which this template does not attempt.
-- * Ciphertext/encrypted fields are opaque. Any field MongoDB (or the
--   Alteryx Service) stores encrypted is landed as-is inside the payload
--   VARIANT -- Snowflake has no way to decrypt it, so such fields can only
--   be carried through unchanged, not filtered, transformed or queried on
--   their plaintext value.
-- ----------------------------------------------------------------------------
