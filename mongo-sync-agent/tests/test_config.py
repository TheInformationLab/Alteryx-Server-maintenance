"""Tests for mongo_sync_agent.config: TOML parsing and validation."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from mongo_sync_agent.config import ConfigError, load_config

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
STANDALONE_PATH = _CONFIG_DIR / "config.example.standalone.toml"
EMBEDDED_PATH = _CONFIG_DIR / "config.example.alteryx-embedded.toml"


def _write_config(tmp_path: Path, collections_toml: str) -> Path:
    """Write a minimal-but-valid config file with the given [[mongo.collections]] block(s)."""
    content = f"""
[agent]
state_db = "state.db"
spool_dir = "spool"
log_dir = "logs"

[mongo]
enabled = true
database = "testdb"
host = "localhost"

{collections_toml}

[s3]
bucket = "test-bucket"
"""
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_standalone_profile_parses():
    cfg = load_config(STANDALONE_PATH)

    assert cfg.mongo_enabled is True
    assert cfg.mongo is not None
    assert cfg.mongo.database == "AlteryxService"
    assert cfg.mongo.host == "mongo01.internal.example.com"
    assert cfg.mongo.username == "alteryx_reader"
    assert cfg.mongo.uri is None

    assert [c.name for c in cfg.collections] == ["AS_Queue", "AS_Jobs"]
    assert cfg.collections[0].mode == "append_only"
    assert cfg.collections[1].mode == "mutable"
    assert cfg.collections[1].watermark_field == "dtModified"

    assert cfg.s3.bucket == "my-alteryx-mongo-sync-bucket"
    assert cfg.s3.region == "eu-west-2"

    assert cfg.logs.enabled is True
    assert [s.name for s in cfg.logs.sources] == ["gallery", "service"]

    assert cfg.hostmetrics.enabled is True
    assert cfg.hostmetrics.disks == ["C:", "D:"]


def test_embedded_profile_parses():
    cfg = load_config(EMBEDDED_PATH)

    assert cfg.mongo_enabled is True
    assert cfg.mongo is not None
    assert cfg.mongo.database == "AlteryxService"
    assert cfg.mongo.uri == "mongodb://controller:CHANGE_ME@localhost:27018/AlteryxService?authSource=admin"
    # host is left at its dataclass default since 'uri' is used instead.
    assert cfg.mongo.host == "localhost"

    assert [c.name for c in cfg.collections] == ["AS_Queue", "AS_Jobs"]
    assert cfg.s3.bucket == "my-alteryx-mongo-sync-bucket"


def test_profiles_differ_only_in_mongo():
    standalone = load_config(STANDALONE_PATH)
    embedded = load_config(EMBEDDED_PATH)

    # The whole point of the two profiles is that [mongo] is the only delta.
    assert standalone.mongo != embedded.mongo

    standalone_sans_mongo = dataclasses.replace(standalone, mongo=None)
    embedded_sans_mongo = dataclasses.replace(embedded, mongo=None)
    assert standalone_sans_mongo == embedded_sans_mongo


def test_bad_mode_raises_config_error(tmp_path):
    path = _write_config(
        tmp_path,
        """
[[mongo.collections]]
name = "coll1"
mode = "invalid"
""",
    )

    with pytest.raises(ConfigError, match="mode"):
        load_config(path)


def test_mutable_without_watermark_field_raises(tmp_path):
    path = _write_config(
        tmp_path,
        """
[[mongo.collections]]
name = "coll1"
mode = "mutable"
""",
    )

    with pytest.raises(ConfigError, match="watermark_field"):
        load_config(path)


def test_gridfs_chunks_refused_by_default(tmp_path):
    path = _write_config(
        tmp_path,
        """
[[mongo.collections]]
name = "fs.chunks"
mode = "append_only"
""",
    )

    with pytest.raises(ConfigError, match="allow_gridfs_chunks"):
        load_config(path)
