-- ============================================================================
-- mongo-sync-agent Snowflake landing layer
-- 01_stage.sql -- storage integration + external stage over the S3 landing
-- bucket that the agent uploads Parquet / gzip-NDJSON files to.
--
-- Run this once per environment (dev/test/prod), then 02-05 build on top of
-- the stage created here.
-- ============================================================================

-- Storage integrations are account-level objects and can only be created (or
-- altered) by a role with the CREATE INTEGRATION privilege -- in practice this
-- means ACCOUNTADMIN, or a custom role that has been explicitly granted that
-- privilege. Everything else in this repo's SQL can be run by a lower-privilege
-- role (e.g. SYSADMIN) once the integration and its grant exist.
USE ROLE ACCOUNTADMIN;

-- Adjust the database/schema context to wherever the landing objects should
-- live before running 02-05, e.g.:
--   USE DATABASE <DATABASE_NAME>;
--   USE SCHEMA <SCHEMA_NAME>;

CREATE STORAGE INTEGRATION IF NOT EXISTS <STORAGE_INTEGRATION_NAME>
    TYPE = EXTERNAL_STAGE
    STORAGE_PROVIDER = 'S3'
    ENABLED = TRUE
    -- The IAM role below must already exist in AWS with a trust policy that
    -- (initially) allows the Snowflake account to assume it. Snowflake will
    -- report the IAM user ARN and external ID it will actually authenticate
    -- as once the integration is created (see DESC STORAGE INTEGRATION
    -- below) -- the trust policy needs a second edit after that to lock it
    -- down to those exact values.
    STORAGE_AWS_ROLE_ARN = 'arn:aws:iam::<AWS_ACCOUNT_ID>:role/<IAM_ROLE_NAME>'
    STORAGE_ALLOWED_LOCATIONS = ('s3://<BUCKET_NAME>/<PREFIX>/');

-- Run this and note STORAGE_AWS_IAM_USER_ARN and STORAGE_AWS_EXTERNAL_ID from
-- the output -- both values must be pasted into the AWS IAM role's trust
-- policy (Condition -> sts:ExternalId, Principal -> the Snowflake IAM user
-- ARN) before the stage below can actually list/read objects in the bucket.
DESC STORAGE INTEGRATION <STORAGE_INTEGRATION_NAME>;

-- Optional: let a lower-privileged role use the integration to create/manage
-- stages and pipes without needing ACCOUNTADMIN for day-to-day work.
-- GRANT USAGE ON INTEGRATION <STORAGE_INTEGRATION_NAME> TO ROLE <ROLE_NAME>;

-- The stage itself can be created by SYSADMIN (or whichever role was granted
-- USAGE above) once the integration exists.
-- USE ROLE <ROLE_NAME>;

CREATE STAGE IF NOT EXISTS <STAGE_NAME>
    STORAGE_INTEGRATION = <STORAGE_INTEGRATION_NAME>
    URL = 's3://<BUCKET_NAME>/<PREFIX>/'
    COMMENT = 'External stage over the mongo-sync-agent S3 landing bucket (mongo/, logs/, hostmetrics/ prefixes).';

-- Sanity check once the AWS-side trust policy has been updated -- this should
-- list the objects the agent has uploaded so far without error.
-- LIST @<STAGE_NAME>;
