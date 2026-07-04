"""Landing formats: transform a raw MongoDB document into a row for the sink.

A landing format owns two things: the target Arrow/Parquet ``schema`` and the
``doc_to_row`` mapping that turns a BSON document (plus per-run context) into a
dict matching that schema. Keeping this behind a small :class:`LandingFormat`
protocol means the extract loop never needs to know how a document is encoded
downstream -- it just calls ``doc_to_row`` for every doc and hands the result
to a :class:`~mongo_sync_agent.sinks.Sink`.

:class:`VariantJsonLanding` is currently the only implementation. It lands each
document as a single Relaxed Extended JSON payload string (for Snowflake
``PARSE_JSON`` / ``VARIANT`` ingestion) alongside a handful of typed metadata
columns used for partition pruning and dedup.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple, Protocol

import pyarrow as pa
from bson import json_util

from .mongo.watermark import WatermarkStrategy


class RowContext(NamedTuple):
    """Per-row context supplied by the extract loop, independent of the doc itself."""

    extracted_at: datetime


class LandingFormat(Protocol):
    """A landing format: an Arrow schema plus a doc -> row mapping.

    Implementations must be stateless with respect to any individual doc (state
    such as the watermark strategy is fine to hold, since it is shared across
    the whole collection's extraction) so that ``doc_to_row`` can be called
    once per document with no ordering constraints.
    """

    schema: pa.Schema

    def doc_to_row(self, doc: dict, ctx: RowContext) -> dict:
        """Map a single MongoDB document to a row dict matching ``schema``."""
        ...


class VariantJsonLanding:
    """VARIANT/JSON landing format: one JSON payload column + typed metadata columns.

    ``payload`` carries the full document as Relaxed Extended JSON, which is
    valid JSON that Snowflake's ``PARSE_JSON`` / ``VARIANT`` type handles
    natively -- ObjectIds become ``{"$oid": "..."}``, dates become
    ``{"$date": "..."}``, Decimal128 becomes ``{"$numberDecimal": "..."}``, and
    binary becomes ``{"$binary": {...}}``.
    """

    schema: pa.Schema = pa.schema(
        [
            pa.field("payload", pa.string()),  # full doc as Relaxed Extended JSON
            pa.field("_id", pa.string()),  # doc _id as hex or str
            pa.field("_watermark", pa.string()),  # canonical watermark for this doc
            pa.field(
                "_extracted_at", pa.timestamp("us", tz="UTC")
            ),  # extraction timestamp
        ]
    )

    def __init__(self, strategy: WatermarkStrategy) -> None:
        self._strategy = strategy

    def doc_to_row(self, doc: dict, ctx: RowContext) -> dict:
        payload = json_util.dumps(
            doc, json_options=json_util.JSONOptions(json_mode=json_util.JSONMode.RELAXED)
        )

        raw_id = doc.get("_id")
        if hasattr(raw_id, "__str__"):
            # For ObjectId, str() returns the 24-hex representation. This also
            # covers plain str/int/UUID _id values used in some collections.
            doc_id = str(raw_id)
        else:
            doc_id = repr(raw_id)

        return {
            "payload": payload,
            "_id": doc_id,
            "_watermark": self._strategy.watermark_column_value(doc),
            "_extracted_at": ctx.extracted_at,
        }


def make_landing(strategy: WatermarkStrategy) -> VariantJsonLanding:
    """Construct the (currently sole) landing format for a collection."""
    return VariantJsonLanding(strategy)
