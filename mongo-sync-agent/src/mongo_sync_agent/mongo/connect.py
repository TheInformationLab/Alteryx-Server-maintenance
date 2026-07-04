"""MongoClient factory.

This module is responsible for creating and managing MongoDB connections.
It is the ONLY code that touches the Mongo connection — it must be purely
parameterised from MongoConfig with ZERO instance-specific code paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pymongo

if TYPE_CHECKING:
    from ..config import MongoConfig


def make_client(cfg: MongoConfig) -> pymongo.MongoClient:
    """Build a MongoClient from cfg.

    If cfg.uri is set: use it directly, passing serverSelectionTimeoutMS.
    Otherwise: build from host/port/username/password/authSource/tls/tlsCAFile.
    Always set serverSelectionTimeoutMS=cfg.server_selection_timeout_ms.

    The returned client is NOT eagerly connected (MongoClient is lazy by default).

    Args:
        cfg: MongoConfig dataclass with connection parameters.

    Returns:
        A configured pymongo.MongoClient instance.
    """
    timeout_ms = cfg.server_selection_timeout_ms

    if cfg.uri:
        # URI overrides all other settings
        return pymongo.MongoClient(
            cfg.uri,
            serverSelectionTimeoutMS=timeout_ms,
        )

    # Build connection string from individual parameters
    kwargs = {
        "host": cfg.host,
        "port": cfg.port,
        "serverSelectionTimeoutMS": timeout_ms,
    }

    # Add authentication if provided
    if cfg.username:
        kwargs["username"] = cfg.username
        kwargs["password"] = cfg.password
        kwargs["authSource"] = cfg.auth_source

    # Add TLS configuration if enabled
    if cfg.tls:
        kwargs["tls"] = True
        if cfg.tls_ca_file:
            kwargs["tlsCAFile"] = cfg.tls_ca_file

    return pymongo.MongoClient(**kwargs)


def ping(client: pymongo.MongoClient) -> bool:
    """Run admin.command('ping') on the client.

    Args:
        client: A pymongo.MongoClient instance.

    Returns:
        True if the ping command succeeds, False on any exception.
    """
    try:
        client.admin.command("ping")
        return True
    except Exception:
        return False
