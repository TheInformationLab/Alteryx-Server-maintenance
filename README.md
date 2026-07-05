# Alteryx-Server-maintenance
Housekeeping and Maintenance tasks to be scheduled on an Alteryx Server

**Updated April 2020**

The Alteryx  Server backup process requires the Server to be shutdown before the backup can be taken. This script is designed to backup the sever into a tempoary folder, archive the Embedded MongoDB and archive to a S3 bucket.

## Subprojects

- **[`mongo-sync-agent/`](mongo-sync-agent/README.md)** — a poll-based agent
  that incrementally replicates Alteryx Server's MongoDB, logs, and host
  metrics into S3 for ingestion into Snowflake. Distributed as a self-contained
  Windows `msa.exe`; see its [release process](mongo-sync-agent/RELEASING.md)
  for how betas and releases are built and published from GitHub tags.

Developed by Paul Houghton @ [The Information Lab](https://www.theinformationlab.co.uk/ "The Information Lab")