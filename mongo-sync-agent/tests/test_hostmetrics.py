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


def test_disk_io_maps_drive_letter_to_physical_drive_counters(mocker):
    # On Windows psutil keys perdisk counters by physical drive, not drive
    # letter, so the configured "C:" must be resolved to its physical drive(s)
    # before the counters are read. Mock the resolution + counters so the test
    # is OS-independent.
    fake_usage = mocker.Mock(total=100, used=40, free=60, percent=40.0)
    fake_io = mocker.Mock(read_bytes=111, write_bytes=222, read_count=3, write_count=4)
    mocker.patch.object(collect_mod.psutil, "disk_usage", return_value=fake_usage)
    mocker.patch.object(
        collect_mod.psutil, "disk_io_counters",
        return_value={"PhysicalDrive1": fake_io},
    )
    mocker.patch.object(collect_mod, "_resolve_perdisk_keys", return_value=["PhysicalDrive1"])

    records = collect_mod.collect(["C:"], host="disk-host")

    io_records = [r for r in records if r["metric"] == "disk_io"]
    assert len(io_records) == 1
    io = io_records[0]
    assert io["path"] == "C:"
    assert (io["read_bytes"], io["write_bytes"], io["read_count"], io["write_count"]) == (111, 222, 3, 4)
    assert "warning" not in io


def test_disk_io_sums_counters_across_a_spanned_volume(mocker):
    # A volume can span multiple physical disks; their counters must be summed.
    fake_usage = mocker.Mock(total=100, used=40, free=60, percent=40.0)
    io_a = mocker.Mock(read_bytes=10, write_bytes=20, read_count=1, write_count=2)
    io_b = mocker.Mock(read_bytes=100, write_bytes=200, read_count=5, write_count=6)
    mocker.patch.object(collect_mod.psutil, "disk_usage", return_value=fake_usage)
    mocker.patch.object(
        collect_mod.psutil, "disk_io_counters",
        return_value={"PhysicalDrive0": io_a, "PhysicalDrive1": io_b},
    )
    mocker.patch.object(
        collect_mod, "_resolve_perdisk_keys",
        return_value=["PhysicalDrive0", "PhysicalDrive1"],
    )

    records = collect_mod.collect(["D:"], host="disk-host")

    io = next(r for r in records if r["metric"] == "disk_io")
    assert (io["read_bytes"], io["write_bytes"], io["read_count"], io["write_count"]) == (110, 220, 6, 8)


def test_disk_io_falls_back_to_zeros_when_mapping_unresolved(mocker):
    # When the drive letter can't be mapped to a physical drive, the record
    # must still be emitted with zeros and a "warning" key.
    fake_usage = mocker.Mock(total=100, used=40, free=60, percent=40.0)
    fake_io = mocker.Mock(read_bytes=999, write_bytes=999, read_count=9, write_count=9)
    mocker.patch.object(collect_mod.psutil, "disk_usage", return_value=fake_usage)
    mocker.patch.object(
        collect_mod.psutil, "disk_io_counters",
        return_value={"PhysicalDrive0": fake_io},
    )
    mocker.patch.object(collect_mod, "_resolve_perdisk_keys", return_value=None)

    records = collect_mod.collect(["Z:"], host="disk-host")

    io = next(r for r in records if r["metric"] == "disk_io")
    assert (io["read_bytes"], io["write_bytes"], io["read_count"], io["write_count"]) == (0, 0, 0, 0)
    assert "warning" in io
