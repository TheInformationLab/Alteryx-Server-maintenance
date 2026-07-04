"""Watermark strategies for incremental MongoDB extraction.

This is the LOAD-BEARING correctness module for the mongo-sync-agent. It defines
how each collection's replication position is derived, filtered on, and advanced.

Three strategies map one-to-one onto the collection modes:

* ``append_only``  -> :class:`ObjectIdWatermark`   (kind ``"objectid"``)
* ``mutable``      -> :class:`TimestampWatermark`  (kind ``"timestamp"``)
* ``full_refresh`` -> :class:`FullRefreshStrategy` (kind ``"full_refresh"``)

The strategy pattern is deliberate: the extract loop must NOT branch on mode.
It asks the strategy for a filter and sort, feeds every written doc to
``observe()``, and reads back ``new_watermark()`` at the end.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import ClassVar

from bson import ObjectId

from ..config import CollectionConfig

# Canonical timestamp format for the "timestamp" watermark kind. Zero-padded and
# fixed-length so lexicographic string ordering matches chronological ordering.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _format_ts(dt: datetime) -> str:
    """Format a datetime as the canonical millisecond-precision UTC string."""
    dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class WatermarkStrategy(ABC):
    """Abstract strategy governing incremental extraction for one collection."""

    kind: ClassVar[str]  # "objectid" | "timestamp" | "full_refresh"

    @abstractmethod
    def build_filter(self, previous: str | None) -> dict:
        """Return a pymongo find() filter dict. previous is the canonical watermark
        string from state, or None (first run / full refresh)."""

    @abstractmethod
    def sort_spec(self) -> list[tuple[str, int]] | None:
        """Cursor sort spec, or None for no sort."""

    @abstractmethod
    def observe(self, doc: dict) -> None:
        """Called for every doc written to the sink. Track the running max."""

    @abstractmethod
    def new_watermark(self) -> str | None:
        """Return the canonical string for the max observed value, or None if nothing
        seen or this is a full_refresh strategy."""

    @abstractmethod
    def watermark_column_value(self, doc: dict) -> str:
        """Return the canonical watermark string for a specific doc (for the Parquet
        column)."""


class ObjectIdWatermark(WatermarkStrategy):
    """Append-only watermark keyed on the monotonic-ish ``_id`` ObjectId.

    Canonical storage is the 24-character hex string of the ObjectId. Hex strings
    of equal-length ObjectIds sort lexicographically identically to native
    ObjectId sort order -- this equivalence is what makes storing the watermark
    as a plain string correct.
    """

    kind: ClassVar[str] = "objectid"

    def __init__(self, cfg: CollectionConfig) -> None:
        self._cfg = cfg
        self._max_id: ObjectId | None = None

    def build_filter(self, previous: str | None) -> dict:
        if previous is None and self._cfg.initial_watermark is None:
            # First run, no configured floor: full initial scan.
            return {}

        # Parse whichever bound we have. A configured initial_watermark is parsed
        # exactly as though it were a stored watermark string (24-hex ObjectId).
        bound = previous if previous is not None else self._cfg.initial_watermark
        prev_obj = ObjectId(bound)
        prev_ts = prev_obj.generation_time  # tz-aware UTC datetime, second granularity
        lb_ts = prev_ts - timedelta(seconds=self._cfg.overlap_seconds)
        lb_obj = ObjectId.from_datetime(lb_ts)  # zero-filled random/counter bytes

        # WHY THE OVERLAP IS MANDATORY
        # ----------------------------
        # ObjectIds are generated on CLIENT NODES, not on the MongoDB server.
        # Within the same second, ObjectIds from different clients are NOT
        # monotonically ordered with respect to their wall-clock insertion order
        # into MongoDB. A document inserted concurrently with our cursor read can
        # have an _id whose timestamp equals our observed max but whose
        # random/counter bytes sort it below that max. A strict "$gt: max_id"
        # filter would then drop that document forever.
        #
        # The overlap re-fetches docs from the last ``overlap_seconds`` seconds.
        # The synthetic lower-bound ObjectId has ZERO random/counter bytes, so any
        # real ObjectId minted in that same second sorts strictly above it and is
        # therefore re-included. Duplicates are harmless: downstream delivery is
        # at-least-once and the Snowflake MERGE on _id is idempotent.
        return {"_id": {"$gt": lb_obj}}

    def sort_spec(self) -> list[tuple[str, int]] | None:
        # The _id index is always present, so this sort is free for the source.
        return [("_id", 1)]

    def observe(self, doc: dict) -> None:
        oid = doc.get("_id")
        if oid is None:
            return
        if self._max_id is None:
            self._max_id = oid
            return
        try:
            if oid > self._max_id:
                self._max_id = oid
        except TypeError:
            # _id is not directly comparable to the current max (e.g. not an
            # ObjectId). Fall back to lexicographic string comparison.
            if str(oid) > str(self._max_id):
                self._max_id = oid

    def new_watermark(self) -> str | None:
        # ObjectId.__str__ returns the 24-hex representation.
        return str(self._max_id) if self._max_id is not None else None

    def watermark_column_value(self, doc: dict) -> str:
        return str(doc.get("_id", ""))


class TimestampWatermark(WatermarkStrategy):
    """Mutable-collection watermark keyed on a configured timestamp field.

    Canonical storage is ``"YYYY-MM-DDTHH:MM:SS.mmmZ"`` (UTC, millisecond
    precision, fixed format). The fixed, zero-padded layout means lexicographic
    string ordering matches chronological ordering.
    """

    kind: ClassVar[str] = "timestamp"

    def __init__(self, cfg: CollectionConfig) -> None:
        self._cfg = cfg
        self._field = cfg.watermark_field
        self._max_val: datetime | None = None

    def build_filter(self, previous: str | None) -> dict:
        if previous is None and self._cfg.initial_watermark is None:
            return {}
        lb_str = previous if previous is not None else self._cfg.initial_watermark
        lb_dt = datetime.strptime(lb_str, _TS_FORMAT).replace(tzinfo=timezone.utc)
        lb_dt = lb_dt - timedelta(seconds=self._cfg.overlap_seconds)
        # Use $gte (not $gt): equal-timestamp writes at millisecond granularity are
        # common in mutable collections, and the overlap already guarantees we
        # re-scan the boundary window. Missing an equal-timestamp doc is worse than
        # re-reading one (dedup happens downstream via idempotent MERGE).
        return {self._field: {"$gte": lb_dt}}

    def sort_spec(self) -> list[tuple[str, int]] | None:
        # Deliberately NO sort. Sorting on an arbitrary mutable field would require
        # a matching index on the source (which we cannot assume) or would hit
        # MongoDB's 100 MB in-memory sort limit on an underpowered box. Instead we
        # track the running max ourselves in observe().
        return None

    def observe(self, doc: dict) -> None:
        field_val = doc.get(self._field)
        if isinstance(field_val, datetime) and (
            self._max_val is None or field_val > self._max_val
        ):
            self._max_val = field_val

    def new_watermark(self) -> str | None:
        if self._max_val is None:
            return None
        return _format_ts(self._max_val)

    def watermark_column_value(self, doc: dict) -> str:
        field_val = doc.get(self._field)
        if isinstance(field_val, datetime):
            return _format_ts(field_val)
        return ""


class FullRefreshStrategy(WatermarkStrategy):
    """Full-refresh strategy: every run rescans the entire collection.

    No watermark is ever persisted; there is nothing to advance.
    """

    kind: ClassVar[str] = "full_refresh"

    def __init__(self, cfg: CollectionConfig) -> None:
        self._cfg = cfg

    def build_filter(self, previous: str | None) -> dict:
        return {}

    def sort_spec(self) -> list[tuple[str, int]] | None:
        return None

    def observe(self, doc: dict) -> None:
        pass

    def new_watermark(self) -> str | None:
        return None

    def watermark_column_value(self, doc: dict) -> str:
        return ""


def make_strategy(cfg: CollectionConfig) -> WatermarkStrategy:
    """Construct the watermark strategy matching ``cfg.mode``."""
    if cfg.mode == "append_only":
        return ObjectIdWatermark(cfg)
    if cfg.mode == "mutable":
        return TimestampWatermark(cfg)
    if cfg.mode == "full_refresh":
        return FullRefreshStrategy(cfg)
    raise ValueError(f"Unknown mode: {cfg.mode}")


def validate_watermark_kind(stored: "Watermark", strategy: WatermarkStrategy) -> None:
    """Raise ConfigError if stored.kind != strategy.kind."""
    from ..config import ConfigError

    if stored.kind != strategy.kind:
        raise ConfigError(
            f"Watermark kind mismatch for collection: stored={stored.kind!r}, "
            f"configured={strategy.kind!r}. Change the mode back or reset the watermark."
        )


# Imported lazily at runtime inside validate_watermark_kind; referenced here only
# for the type annotation under TYPE_CHECKING to avoid a circular import.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import Watermark
