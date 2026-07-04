"""Tests for the watermark strategies -- the load-bearing correctness module.

These lock down the invariants that make incremental extraction correct:

* the ObjectId overlap window must re-include same-second concurrent inserts;
* canonical string forms must sort identically to their native types;
* ``observe()`` must track the running max regardless of arrival order;
* full_refresh never persists a watermark;
* a stored/configured kind mismatch is rejected loudly.
"""

from __future__ import annotations

import random
import re
import struct
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from mongo_sync_agent.config import CollectionConfig, ConfigError
from mongo_sync_agent.mongo.watermark import (
    FullRefreshStrategy,
    ObjectIdWatermark,
    TimestampWatermark,
    validate_watermark_kind,
)
from mongo_sync_agent.state import Watermark

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _oid_cfg(**kw) -> CollectionConfig:
    kw.setdefault("overlap_seconds", 30)
    return CollectionConfig(name="test", mode="append_only", **kw)


def _ts_cfg(**kw) -> CollectionConfig:
    kw.setdefault("overlap_seconds", 30)
    kw.setdefault("watermark_field", "updated_at")
    return CollectionConfig(name="test", mode="mutable", **kw)


# --------------------------------------------------------------------------- #
# ObjectIdWatermark
# --------------------------------------------------------------------------- #

def test_objectid_same_second_overlap():
    # Two ObjectIds with IDENTICAL generation_time but different random/counter
    # bytes, inserted in two separate "runs" of extraction.
    ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    id_a = ObjectId.from_datetime(ts)  # zero random/counter bytes

    # Manually construct id_b with the same timestamp second but non-zero
    # random/counter bytes (as a real concurrently-inserted _id would have).
    ts_bytes = struct.pack(">I", int(ts.timestamp()))
    id_b_bytes = ts_bytes + b"\xaa\xbb\xcc\xdd\xee" + b"\x00\x00\x01"
    id_b = ObjectId(id_b_bytes.hex())

    cfg = _oid_cfg()
    strategy = ObjectIdWatermark(cfg)

    # Run 1 result: watermark advanced to id_a.
    watermark_hex = str(id_a)

    # Run 2: build the filter from that watermark.
    filt = strategy.build_filter(watermark_hex)
    lb_oid = filt["_id"]["$gt"]

    # The overlap lower bound sits BEFORE both ids so id_b is recaptured.
    assert lb_oid < id_b, f"Lower bound {lb_oid} should be less than id_b {id_b}"
    assert lb_oid < id_a, "Lower bound should be less than the previous watermark"


def test_objectid_first_run_no_filter():
    strategy = ObjectIdWatermark(_oid_cfg())
    assert strategy.build_filter(None) == {}


def test_objectid_initial_watermark():
    seed = ObjectId.from_datetime(datetime(2024, 1, 1, tzinfo=timezone.utc))
    strategy = ObjectIdWatermark(_oid_cfg(initial_watermark=str(seed)))
    filt = strategy.build_filter(None)
    assert "_id" in filt and "$gt" in filt["_id"]
    assert isinstance(filt["_id"]["$gt"], ObjectId)


def test_objectid_observe_tracks_max():
    oids = [ObjectId() for _ in range(50)]
    expected_max = max(oids)
    shuffled = oids[:]
    random.shuffle(shuffled)

    strategy = ObjectIdWatermark(_oid_cfg())
    for oid in shuffled:
        strategy.observe({"_id": oid})

    assert strategy.new_watermark() == str(expected_max)


def test_objectid_sort_spec():
    assert ObjectIdWatermark(_oid_cfg()).sort_spec() == [("_id", 1)]


def test_objectid_canonical_string_ordering():
    # 24-hex string ordering must match native ObjectId ordering. Include
    # same-second ids so the random/counter tail participates in the compare.
    base = datetime(2024, 6, 1, 8, 0, 0, tzinfo=timezone.utc)
    oids = [ObjectId() for _ in range(20)]
    oids += [ObjectId.from_datetime(base) for _ in range(5)]
    oids += [ObjectId() for _ in range(20)]

    by_object = [str(o) for o in sorted(oids)]
    by_string = sorted(str(o) for o in oids)
    assert by_object == by_string


def test_objectid_new_watermark_none_when_nothing_observed():
    assert ObjectIdWatermark(_oid_cfg()).new_watermark() is None


# --------------------------------------------------------------------------- #
# TimestampWatermark
# --------------------------------------------------------------------------- #

def test_timestamp_build_filter():
    strategy = TimestampWatermark(_ts_cfg(overlap_seconds=30))
    previous = "2024-01-15T12:00:00.000Z"
    filt = strategy.build_filter(previous)

    expected = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc) - timedelta(
        seconds=30
    )
    assert set(filt) == {"updated_at"}
    assert set(filt["updated_at"]) == {"$gte"}
    assert filt["updated_at"]["$gte"] == expected


def test_timestamp_first_run_no_filter():
    assert TimestampWatermark(_ts_cfg()).build_filter(None) == {}


def test_timestamp_no_sort():
    assert TimestampWatermark(_ts_cfg()).sort_spec() is None


def test_timestamp_observe_tracks_max():
    base = datetime(2023, 3, 10, 0, 0, 0, tzinfo=timezone.utc)
    times = [base + timedelta(seconds=i, milliseconds=i * 7) for i in range(40)]
    expected_max = max(times)
    shuffled = times[:]
    random.shuffle(shuffled)

    strategy = TimestampWatermark(_ts_cfg())
    for t in shuffled:
        strategy.observe({"updated_at": t})

    ms = expected_max.microsecond // 1000
    expected_iso = expected_max.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"
    assert strategy.new_watermark() == expected_iso


def test_timestamp_observe_ignores_non_datetime():
    strategy = TimestampWatermark(_ts_cfg())
    strategy.observe({"updated_at": "not-a-datetime"})
    strategy.observe({"updated_at": None})
    strategy.observe({})  # field missing entirely
    assert strategy.new_watermark() is None


def test_timestamp_canonical_format():
    # Single-digit month/day/hour must be zero-padded to a fixed-width string.
    dt = datetime(2024, 3, 5, 4, 7, 9, 12000, tzinfo=timezone.utc)
    strategy = TimestampWatermark(_ts_cfg())
    strategy.observe({"updated_at": dt})
    wm = strategy.new_watermark()

    assert wm == "2024-03-05T04:07:09.012Z"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", wm)
    # Round-trips through the canonical parse format used by build_filter.
    datetime.strptime(wm, _TS_FORMAT)

    # Lexicographic ordering matches chronological ordering.
    later = datetime(2024, 12, 25, 23, 59, 59, 999000, tzinfo=timezone.utc)
    s2 = TimestampWatermark(_ts_cfg())
    s2.observe({"updated_at": later})
    assert wm < s2.new_watermark()


# --------------------------------------------------------------------------- #
# FullRefreshStrategy
# --------------------------------------------------------------------------- #

def _fr_cfg() -> CollectionConfig:
    return CollectionConfig(name="test", mode="full_refresh")


@pytest.mark.parametrize("previous", [None, "", "anything", "507f1f77bcf86cd799439011"])
def test_full_refresh_always_empty_filter(previous):
    assert FullRefreshStrategy(_fr_cfg()).build_filter(previous) == {}


def test_full_refresh_no_watermark():
    strategy = FullRefreshStrategy(_fr_cfg())
    assert strategy.new_watermark() is None
    strategy.observe({"_id": ObjectId(), "x": 1})  # must not change anything
    assert strategy.new_watermark() is None
    assert strategy.sort_spec() is None


# --------------------------------------------------------------------------- #
# validate_watermark_kind
# --------------------------------------------------------------------------- #

def test_kind_mismatch_raises():
    stored = Watermark(
        kind="timestamp", value="x", updated_at="now", run_id="r1"
    )
    strategy = ObjectIdWatermark(_oid_cfg())  # kind == "objectid"
    with pytest.raises(ConfigError):
        validate_watermark_kind(stored, strategy)


def test_kind_match_does_not_raise():
    stored = Watermark(
        kind="objectid", value=str(ObjectId()), updated_at="now", run_id="r1"
    )
    validate_watermark_kind(stored, ObjectIdWatermark(_oid_cfg()))
