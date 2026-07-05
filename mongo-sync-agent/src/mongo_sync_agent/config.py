"""TOML configuration loading for the mongo-sync-agent.

Parses a TOML config file (stdlib ``tomllib``, Python 3.11+) into a tree of
frozen dataclasses. All validation errors are raised as :class:`ConfigError`
with a message naming the offending field.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

_DEFAULT_LOG_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "mongo-sync-agent",
    "logs",
)

_VALID_MODES = ("append_only", "mutable", "full_refresh")


class ConfigError(Exception):
    """Raised when the config file is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class MongoConfig:
    database: str
    uri: str | None = None  # full URI overrides host/port/auth when set
    host: str = "localhost"
    port: int = 27017
    username: str | None = None
    password: str | None = None
    auth_source: str = "admin"
    tls: bool = False
    tls_ca_file: str | None = None
    server_selection_timeout_ms: int = 5000


@dataclass(frozen=True)
class CollectionConfig:
    name: str
    mode: Literal["append_only", "mutable", "full_refresh"]
    watermark_field: str | None = None  # for mutable mode; None for append_only / full_refresh
    overlap_seconds: int = 30
    batch_size: int = 1000
    initial_watermark: str | None = None
    allow_gridfs_chunks: bool = False


@dataclass(frozen=True)
class S3Config:
    bucket: str
    region: str = "us-east-1"
    prefix: str = ""


@dataclass(frozen=True)
class LogSourceConfig:
    name: str
    path_glob: str
    encoding: str = "utf-8"
    max_bytes_per_poll: int = 8_388_608  # 8 MB


@dataclass(frozen=True)
class LogsConfig:
    enabled: bool = True
    sources: list[LogSourceConfig] = field(default_factory=list)
    gc_days: int = 14


@dataclass(frozen=True)
class HostMetricsConfig:
    enabled: bool = True
    disks: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentConfig:
    state_db: str
    spool_dir: str
    log_dir: str
    log_level: str
    mongo_enabled: bool
    mongo: MongoConfig | None
    collections: list[CollectionConfig]
    s3: S3Config
    logs: LogsConfig
    hostmetrics: HostMetricsConfig
    # Logical identifier for the host this agent runs on. Stamped onto every
    # shipped log line and host-metric record so rows from multiple Server
    # hosts landing in the same S3/Snowflake can be told apart. When unset
    # (None), the runner falls back to the machine's network name
    # (socket.gethostname()).
    host_id: str | None = None


def _require(table: dict[str, Any], key: str, field_name: str, expected_type: type) -> Any:
    if key not in table:
        raise ConfigError(f"missing required field '{field_name}'")
    value = table[key]
    if not isinstance(value, expected_type) or (expected_type is str and value == ""):
        raise ConfigError(f"field '{field_name}' must be a non-empty {expected_type.__name__}")
    return value


def _optional(table: dict[str, Any], key: str, field_name: str, expected_type: type, default: Any) -> Any:
    if key not in table:
        return default
    value = table[key]
    if value is not None and not isinstance(value, expected_type):
        raise ConfigError(f"field '{field_name}' must be a {expected_type.__name__}")
    return value


def _parse_mongo(raw: dict[str, Any] | None) -> tuple[bool, MongoConfig | None, list[CollectionConfig]]:
    if raw is None:
        return False, None, []

    enabled = _optional(raw, "enabled", "mongo.enabled", bool, True)

    if not enabled:
        return False, None, []

    database = _require(raw, "database", "mongo.database", str)
    uri = _optional(raw, "uri", "mongo.uri", str, None)
    host = _optional(raw, "host", "mongo.host", str, "localhost")
    port = _optional(raw, "port", "mongo.port", int, 27017)
    username = _optional(raw, "username", "mongo.username", str, None)
    password = _optional(raw, "password", "mongo.password", str, None)
    auth_source = _optional(raw, "auth_source", "mongo.auth_source", str, "admin")
    tls = _optional(raw, "tls", "mongo.tls", bool, False)
    tls_ca_file = _optional(raw, "tls_ca_file", "mongo.tls_ca_file", str, None)
    server_selection_timeout_ms = _optional(
        raw, "server_selection_timeout_ms", "mongo.server_selection_timeout_ms", int, 5000
    )

    mongo = MongoConfig(
        database=database,
        uri=uri,
        host=host,
        port=port,
        username=username,
        password=password,
        auth_source=auth_source,
        tls=tls,
        tls_ca_file=tls_ca_file,
        server_selection_timeout_ms=server_selection_timeout_ms,
    )

    collections_raw = raw.get("collections", [])
    if not isinstance(collections_raw, list):
        raise ConfigError("field 'mongo.collections' must be an array of tables")

    collections = [_parse_collection(entry, idx) for idx, entry in enumerate(collections_raw)]

    return True, mongo, collections


def _parse_collection(raw: Any, idx: int) -> CollectionConfig:
    prefix = f"mongo.collections[{idx}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"field '{prefix}' must be a table")

    name = _require(raw, "name", f"{prefix}.name", str)
    mode = _require(raw, "mode", f"{prefix}.mode", str)
    if mode not in _VALID_MODES:
        raise ConfigError(
            f"field '{prefix}.mode' must be one of {_VALID_MODES}, got '{mode}'"
        )

    watermark_field = _optional(raw, "watermark_field", f"{prefix}.watermark_field", str, None)
    if mode == "mutable" and not watermark_field:
        raise ConfigError(f"field '{prefix}.watermark_field' is required when mode is 'mutable'")

    overlap_seconds = _optional(raw, "overlap_seconds", f"{prefix}.overlap_seconds", int, 30)
    batch_size = _optional(raw, "batch_size", f"{prefix}.batch_size", int, 1000)
    initial_watermark = _optional(raw, "initial_watermark", f"{prefix}.initial_watermark", str, None)
    allow_gridfs_chunks = _optional(
        raw, "allow_gridfs_chunks", f"{prefix}.allow_gridfs_chunks", bool, False
    )

    if name.endswith(".chunks") and not allow_gridfs_chunks:
        raise ConfigError(
            f"field '{prefix}.name' ('{name}') looks like a GridFS chunks collection; "
            f"set '{prefix}.allow_gridfs_chunks = true' to sync it explicitly"
        )

    return CollectionConfig(
        name=name,
        mode=mode,  # type: ignore[arg-type]
        watermark_field=watermark_field,
        overlap_seconds=overlap_seconds,
        batch_size=batch_size,
        initial_watermark=initial_watermark,
        allow_gridfs_chunks=allow_gridfs_chunks,
    )


def _parse_s3(raw: dict[str, Any] | None) -> S3Config:
    if raw is None:
        raise ConfigError("missing required section '[s3]'")

    bucket = _require(raw, "bucket", "s3.bucket", str)
    region = _optional(raw, "region", "s3.region", str, "us-east-1")
    prefix = _optional(raw, "prefix", "s3.prefix", str, "")

    return S3Config(bucket=bucket, region=region, prefix=prefix)


def _parse_log_source(raw: Any, idx: int) -> LogSourceConfig:
    prefix = f"logs.sources[{idx}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"field '{prefix}' must be a table")

    name = _require(raw, "name", f"{prefix}.name", str)
    path_glob = _require(raw, "path_glob", f"{prefix}.path_glob", str)
    encoding = _optional(raw, "encoding", f"{prefix}.encoding", str, "utf-8")
    max_bytes_per_poll = _optional(
        raw, "max_bytes_per_poll", f"{prefix}.max_bytes_per_poll", int, 8_388_608
    )

    return LogSourceConfig(
        name=name,
        path_glob=path_glob,
        encoding=encoding,
        max_bytes_per_poll=max_bytes_per_poll,
    )


def _parse_logs(raw: dict[str, Any] | None) -> LogsConfig:
    if raw is None:
        return LogsConfig()

    enabled = _optional(raw, "enabled", "logs.enabled", bool, True)
    gc_days = _optional(raw, "gc_days", "logs.gc_days", int, 14)

    sources_raw = raw.get("sources", [])
    if not isinstance(sources_raw, list):
        raise ConfigError("field 'logs.sources' must be an array of tables")

    sources = [_parse_log_source(entry, idx) for idx, entry in enumerate(sources_raw)]

    return LogsConfig(enabled=enabled, sources=sources, gc_days=gc_days)


def _parse_hostmetrics(raw: dict[str, Any] | None) -> HostMetricsConfig:
    if raw is None:
        return HostMetricsConfig()

    enabled = _optional(raw, "enabled", "hostmetrics.enabled", bool, True)

    disks_raw = raw.get("disks", [])
    if not isinstance(disks_raw, list) or not all(isinstance(d, str) for d in disks_raw):
        raise ConfigError("field 'hostmetrics.disks' must be an array of strings")

    return HostMetricsConfig(enabled=enabled, disks=list(disks_raw))


def load_config(path: Path) -> AgentConfig:
    """Load and validate an :class:`AgentConfig` from a TOML file at ``path``.

    Raises :class:`ConfigError` if the file cannot be read/parsed or fails
    validation.
    """
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"failed to parse TOML config at {path}: {exc}") from exc

    agent_raw = raw.get("agent")
    if agent_raw is None:
        raise ConfigError("missing required section '[agent]'")

    state_db = _require(agent_raw, "state_db", "agent.state_db", str)
    spool_dir = _require(agent_raw, "spool_dir", "agent.spool_dir", str)
    log_dir = _optional(agent_raw, "log_dir", "agent.log_dir", str, _DEFAULT_LOG_DIR)
    log_level = _optional(agent_raw, "log_level", "agent.log_level", str, "WARNING")
    host_id = _optional(agent_raw, "host_id", "agent.host_id", str, None)

    mongo_enabled, mongo, collections = _parse_mongo(raw.get("mongo"))
    s3_cfg = _parse_s3(raw.get("s3"))
    logs_cfg = _parse_logs(raw.get("logs"))
    hostmetrics_cfg = _parse_hostmetrics(raw.get("hostmetrics"))

    return AgentConfig(
        state_db=state_db,
        spool_dir=spool_dir,
        log_dir=log_dir,
        log_level=log_level,
        mongo_enabled=mongo_enabled,
        mongo=mongo,
        collections=collections,
        s3=s3_cfg,
        logs=logs_cfg,
        hostmetrics=hostmetrics_cfg,
        host_id=host_id,
    )
