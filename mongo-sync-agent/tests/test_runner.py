"""Tests for top-level run-cycle orchestration (``runner.run_cycle``).

These verify the orchestration contract rather than the extraction internals
(which have their own tests): a failure in one module must not prevent the
others from running, the process exit code reflects whether any unit failed,
``dry_run`` suppresses S3 uploads, and the recorded run summary carries the
per-unit docs/bytes totals.

The three extraction entry points are stubbed at their defining modules
(``run_cycle`` imports them lazily by name), so no real MongoDB, log files, or
psutil sampling is required. The S3 client is the on-disk ``FakeS3Client`` from
``conftest`` via the ``fake_s3`` fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mongo_sync_agent.hostmetrics.collect as collect_mod
import mongo_sync_agent.logs.ship as ship_mod
import mongo_sync_agent.mongo.extract as extract_mod
from mongo_sync_agent import runner
from mongo_sync_agent.config import (
    AgentConfig,
    CollectionConfig,
    HostMetricsConfig,
    LogsConfig,
    LogSourceConfig,
    MongoConfig,
    S3Config,
)
from mongo_sync_agent.runner_types import UnitResult
from mongo_sync_agent.state import StateStore

from .conftest import FAKE_BUCKET


def _make_cfg(
    tmp_path: Path,
    *,
    mongo_enabled: bool = True,
    logs_enabled: bool = True,
    hostmetrics_enabled: bool = False,
) -> AgentConfig:
    """Build an AgentConfig rooted at tmp_path with the given modules toggled."""
    # run_cycle writes its single-instance lock file into spool_dir *before*
    # setup_spool runs, so the base spool directory must already exist.
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    return AgentConfig(
        state_db=str(tmp_path / "state.db"),
        spool_dir=str(spool_dir),
        log_dir=str(tmp_path / "logs"),
        mongo_enabled=mongo_enabled,
        mongo=MongoConfig(database="testdb") if mongo_enabled else None,
        collections=[CollectionConfig(name="widgets", mode="append_only")],
        s3=S3Config(bucket=FAKE_BUCKET, region="us-east-1", prefix=""),
        logs=LogsConfig(
            enabled=logs_enabled,
            sources=[LogSourceConfig(name="gallery", path_glob=str(tmp_path / "*.csv"))],
        ),
        hostmetrics=HostMetricsConfig(enabled=hostmetrics_enabled, disks=[]),
    )


def _capture_summary(monkeypatch):
    """Patch StateStore.record_run to capture the RunSummary, returning a holder."""
    captured: list = []
    real = StateStore.record_run

    def _cap(self, summary):
        captured.append(summary)
        return real(self, summary)

    monkeypatch.setattr(StateStore, "record_run", _cap)
    return captured


def test_one_failing_module_does_not_stop_others(fake_s3, tmp_path, monkeypatch):
    # Mongo module fails (returns a failed unit); the logs module must still run.
    monkeypatch.setattr(
        extract_mod,
        "extract_all",
        lambda *a, **k: [UnitResult(unit="widgets", status="failed", error="boom")],
    )

    logs_ran = {"called": False}

    def fake_ship_all(*a, **k):
        logs_ran["called"] = True
        return [UnitResult(unit="gallery", status="ok", docs=3)]

    monkeypatch.setattr(ship_mod, "ship_all", fake_ship_all)

    cfg = _make_cfg(tmp_path)
    exit_code = runner.run_cycle(cfg)

    assert logs_ran["called"], "logs module must run even though mongo failed"
    assert exit_code == 1  # a failed unit -> exit 1


def test_exit_code_0_all_ok(fake_s3, tmp_path, monkeypatch):
    monkeypatch.setattr(
        extract_mod, "extract_all", lambda *a, **k: [UnitResult("widgets", "ok", docs=1)]
    )
    monkeypatch.setattr(
        ship_mod, "ship_all", lambda *a, **k: [UnitResult("gallery", "ok", docs=1)]
    )

    exit_code = runner.run_cycle(_make_cfg(tmp_path))

    assert exit_code == 0


def test_exit_code_1_any_failed(fake_s3, tmp_path, monkeypatch):
    monkeypatch.setattr(
        extract_mod, "extract_all", lambda *a, **k: [UnitResult("widgets", "ok", docs=1)]
    )
    monkeypatch.setattr(
        ship_mod,
        "ship_all",
        lambda *a, **k: [UnitResult("gallery", "failed", error="disk full")],
    )

    exit_code = runner.run_cycle(_make_cfg(tmp_path))

    assert exit_code == 1


def test_dry_run_no_upload(fake_s3, tmp_path, monkeypatch):
    # Exercise the runner's own upload-capable path (hostmetrics is uploaded
    # inline in run_cycle) with dry_run=True and assert nothing is uploaded.
    fake_client, _uploader = fake_s3
    monkeypatch.setattr(
        collect_mod,
        "collect",
        lambda disks: [{"metric": "cpu_percent_1s", "value": 1.0, "ts": "2024-01-01T00:00:00Z"}],
    )

    cfg = _make_cfg(
        tmp_path, mongo_enabled=False, logs_enabled=False, hostmetrics_enabled=True
    )
    exit_code = runner.run_cycle(cfg, only={"hostmetrics"}, dry_run=True)

    assert exit_code == 0
    assert fake_client.upload_calls == [], "dry_run must not upload to S3"


def test_dry_run_control_uploads_when_not_dry(fake_s3, tmp_path, monkeypatch):
    # Control for test_dry_run_no_upload: the same path DOES upload when not dry.
    fake_client, _uploader = fake_s3
    monkeypatch.setattr(
        collect_mod,
        "collect",
        lambda disks: [{"metric": "cpu_percent_1s", "value": 1.0, "ts": "2024-01-01T00:00:00Z"}],
    )

    cfg = _make_cfg(
        tmp_path, mongo_enabled=False, logs_enabled=False, hostmetrics_enabled=True
    )
    exit_code = runner.run_cycle(cfg, only={"hostmetrics"}, dry_run=False)

    assert exit_code == 0
    assert len(fake_client.upload_calls) == 1


def test_summary_counts_correct(fake_s3, tmp_path, monkeypatch):
    monkeypatch.setattr(
        extract_mod,
        "extract_all",
        lambda *a, **k: [UnitResult("widgets", "ok", docs=10, bytes_uploaded=100)],
    )
    monkeypatch.setattr(
        ship_mod,
        "ship_all",
        lambda *a, **k: [UnitResult("gallery", "ok", docs=5, bytes_uploaded=50)],
    )
    captured = _capture_summary(monkeypatch)

    exit_code = runner.run_cycle(_make_cfg(tmp_path))

    assert exit_code == 0
    assert len(captured) == 1
    summary = captured[0]

    units = {u.unit: u for u in summary.units}
    assert units["widgets"].docs == 10
    assert units["widgets"].bytes_uploaded == 100
    assert units["gallery"].docs == 5
    assert units["gallery"].bytes_uploaded == 50
    assert sum(u.docs for u in summary.units) == 15
    assert sum(u.bytes_uploaded for u in summary.units) == 150
