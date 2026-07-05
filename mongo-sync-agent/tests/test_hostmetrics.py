"""Tests for mongo_sync_agent.hostmetrics.collect.

The important behavioural contract for the beta is that *every* metric record
carries the ``host`` identifier, so metrics from multiple Server hosts landing
in the same Snowflake table can be attributed to the machine they came from.
"""

from __future__ import annotations

from mongo_sync_agent.hostmetrics import collect as collect_mod


def test_every_record_carries_host():
    # No disks configured keeps this fast and OS-independent; CPU + memory
    # records are always produced and must both carry the host.
    records = collect_mod.collect([], host="test-host-01")

    assert records, "expected at least the cpu and memory records"
    assert all(r.get("host") == "test-host-01" for r in records), (
        "every host-metric record must be stamped with the host identifier"
    )
    metrics = {r["metric"] for r in records}
    assert {"cpu_percent_1s", "memory"} <= metrics


def test_disk_records_also_carry_host(mocker):
    # Force a disk to resolve so the disk_usage / disk_io records are exercised.
    fake_usage = mocker.Mock(total=100, used=40, free=60, percent=40.0)
    mocker.patch.object(collect_mod.psutil, "disk_usage", return_value=fake_usage)
    mocker.patch.object(collect_mod.psutil, "disk_io_counters", return_value={})

    records = collect_mod.collect(["C:"], host="disk-host")

    disk_records = [r for r in records if r["metric"] in ("disk_usage", "disk_io")]
    assert disk_records, "expected disk_usage and disk_io records"
    assert all(r["host"] == "disk-host" for r in disk_records)
