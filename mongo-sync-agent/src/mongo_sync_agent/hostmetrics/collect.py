"""psutil-based host metrics collection."""

import logging
from datetime import datetime, timezone

import psutil

logger = logging.getLogger(__name__)


def collect(disks: list[str]) -> list[dict]:
    """Return a list of metric records (one per type). Each record is a plain dict
    suitable for JSON serialisation. All timestamps are ISO-8601 UTC strings.

    Include:
    - {"metric": "cpu_percent_1s", "value": psutil.cpu_percent(interval=1), "ts": ...}
    - {"metric": "memory", "total": ..., "available": ..., "used": ..., "percent": ..., "ts": ...}
      from psutil.virtual_memory()
    - For each disk in disks that exists (skip with warning if not found):
      {"metric": "disk_usage", "path": disk, "total": ..., "used": ..., "free": ..., "percent": ..., "ts": ...}
      {"metric": "disk_io", "path": disk, "read_bytes": ..., "write_bytes": ...,
       "read_count": ..., "write_count": ..., "ts": ...}
      disk_io from psutil.disk_io_counters(perdisk=True) — match disk to partition;
      if not found, emit a record with all zeros and a "warning" key.
    """
    records = []

    # CPU metrics
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        records.append({
            "metric": "cpu_percent_1s",
            "value": cpu_percent,
            "ts": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        logger.warning(f"Failed to collect CPU metrics: {e}")

    # Memory metrics
    try:
        mem = psutil.virtual_memory()
        records.append({
            "metric": "memory",
            "total": mem.total,
            "available": mem.available,
            "used": mem.used,
            "percent": mem.percent,
            "ts": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        logger.warning(f"Failed to collect memory metrics: {e}")

    # Get disk IO counters once (for all disks)
    disk_io_counters = {}
    try:
        disk_io_counters = psutil.disk_io_counters(perdisk=True) or {}
    except Exception as e:
        logger.warning(f"Failed to collect disk IO counters: {e}")

    # Disk metrics
    for disk in disks:
        # Verify disk exists and get usage
        try:
            usage = psutil.disk_usage(disk)
        except Exception as e:
            logger.warning(f"Disk {disk} not found or inaccessible: {e}")
            continue

        # Disk usage record
        records.append({
            "metric": "disk_usage",
            "path": disk,
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": usage.percent,
            "ts": datetime.now(timezone.utc).isoformat()
        })

        # Disk IO record
        try:
            io_data = disk_io_counters.get(disk)
            if io_data:
                records.append({
                    "metric": "disk_io",
                    "path": disk,
                    "read_bytes": io_data.read_bytes,
                    "write_bytes": io_data.write_bytes,
                    "read_count": io_data.read_count,
                    "write_count": io_data.write_count,
                    "ts": datetime.now(timezone.utc).isoformat()
                })
            else:
                # Not found — emit with zeros and warning
                records.append({
                    "metric": "disk_io",
                    "path": disk,
                    "read_bytes": 0,
                    "write_bytes": 0,
                    "read_count": 0,
                    "write_count": 0,
                    "warning": f"Disk IO data not found for {disk}",
                    "ts": datetime.now(timezone.utc).isoformat()
                })
        except Exception as e:
            logger.warning(f"Failed to collect disk IO for {disk}: {e}")

    return records
