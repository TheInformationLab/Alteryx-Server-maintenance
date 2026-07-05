"""MongoClient factory.

This module is responsible for creating and managing MongoDB connections.
It is the ONLY code that touches the Mongo connection — it must be purely
parameterised from MongoConfig with ZERO instance-specific code paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pymongo
from loguru import logger

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
        logger.debug("Creating MongoClient from URI (timeout_ms={})", timeout_ms)
        client = pymongo.MongoClient(cfg.uri, serverSelectionTimeoutMS=timeout_ms)
        logger.debug("MongoClient created (URI mode, db={})", cfg.database)
        return client

    logger.debug(
        "Creating MongoClient: host={} port={} tls={} auth={} timeout_ms={}",
        cfg.host, cfg.port, cfg.tls, bool(cfg.username), timeout_ms,
    )
    kwargs = {
        "host": cfg.host,
        "port": cfg.port,
        "serverSelectionTimeoutMS": timeout_ms,
    }

    if cfg.username:
        kwargs["username"] = cfg.username
        kwargs["password"] = cfg.password
        kwargs["authSource"] = cfg.auth_source

    if cfg.tls:
        kwargs["tls"] = True
        if cfg.tls_ca_file:
            kwargs["tlsCAFile"] = cfg.tls_ca_file

    client = pymongo.MongoClient(**kwargs)
    logger.debug("MongoClient created (host mode, db={})", cfg.database)
    return client


def ping(client: pymongo.MongoClient) -> bool:
    """Run admin.command('ping') on the client.

    Args:
        client: A pymongo.MongoClient instance.

    Returns:
        True if the ping command succeeds, False on any exception.
    """
    try:
        client.admin.command("ping")
        logger.debug("MongoDB ping OK")
        return True
    except Exception as exc:
        logger.warning("MongoDB ping failed: {}", exc)
        return False
