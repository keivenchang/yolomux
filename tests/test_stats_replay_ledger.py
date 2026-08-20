# SPDX-FileCopyrightText: Copyright (c) 2026 Keiven Chang. All rights reserved.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The durable invalidation ledger: what records work, and what is allowed to retire it.

A ledger that records the wrong bucket, or retires a row for the wrong reason, is worse than no
ledger: it reports the ring reconciled while a contradicted aggregate is still being served. Every
case here is about one of those two failures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yolomux_lib.stats_current import materializer
from yolomux_lib.stats_current import service as service_module
from yolomux_lib.stats_current import storage
from yolomux_lib.stats_current.storage import CoverageEpoch
from yolomux_lib.stats_current.storage import Observation
from yolomux_lib.stats_current.storage import UnavailableSpan
from yolomux_lib.stats_current.storage import UsageAtom
from yolomux_lib.stats_current.storage import UsageAtomTombstone

RESOLUTION = 60
RANGE_SECONDS = 3_600


@pytest.fixture
def store(tmp_path):
    with storage.Store.open(tmp_path / storage.DATABASE_FILENAME) as opened:
        yield opened


def _pending(store_obj: storage.Store) -> set[tuple[int, int]]:
    return {
        (int(row[0]), int(row[1]))
        for row in store_obj._connection().execute(
            "SELECT resolution_seconds, bucket_start FROM ring_invalidations WHERE applied_at IS NULL"
        )
    }


def _pending_rows(store_obj: storage.Store) -> set[tuple[int, int, int]]:
    """Including the generation, because the ledger is keyed by it.

    Comparing only (resolution, bucket) hides a NEW invalidation for a bucket that already had one
    from an earlier mutation -- which is exactly the tombstone case, since the atom's own append
    invalidated the same bucket one generation earlier.
    """
    return {
        (int(row[0]), int(row[1]), int(row[2]))
        for row in store_obj._connection().execute(
            "SELECT resolution_seconds, bucket_start, source_generation FROM ring_invalidations "
            "WHERE applied_at IS NULL"
        )
    }


def _pending_generations(store_obj: storage.Store) -> set[int]:
    return {
        int(row[0])
        for row in store_obj._connection().execute(
            "SELECT source_generation FROM ring_invalidations WHERE applied_at IS NULL"
        )
    }


def _cursor(store_obj: storage.Store) -> dict[int, tuple[float, int]]:
    return {
        int(row[0]): (float(row[1]), int(row[2]))
        for row in store_obj._connection().execute(
            "SELECT resolution_seconds, folded_through_observed_at, folded_source_generation "
            "FROM ring_replay_cursor"
        )
    }


def _observation(event_id: str, observed_at: float) -> Observation:
    return Observation(
        event_id=event_id, family="cpu", source_id="host", observed_at=observed_at,
        epoch_id="epoch-a", owner_generation=1,
        payload={"process_percent": 1, "system_percent": 2},
    )


def _usage(event_id: str, observed_at: float, thread_id: str = "t1") -> UsageAtom:
    return UsageAtom(
        event_id=event_id, direction="input", modality="text", cache_role="none", unit="tokens",
        observed_at=observed_at,
        payload={
            "quantity": 5.0, "provider": "openai", "model": "gpt", "thread_id": thread_id,
            "execution_source": "codex",
        },
    )


def _tombstone(event_id: str, observed_at: float, thread_id: str = "t1") -> UsageAtomTombstone:
    return UsageAtomTombstone(
        event_id=event_id, direction="input", modality="text", cache_role="none", unit="tokens",
        observed_at=observed_at, quantity=5.0, provider="openai", model="gpt", thread_id=thread_id,
    )


def _bucket(bucket_start: int, value: int = 1) -> storage.RingBucketWrite:
    return storage.RingBucketWrite(
        resolution_seconds=RESOLUTION, bucket_start=bucket_start,
        bucket_json=json.dumps({
            "series": {"v": {"value": value, "source_count": 1,
                             "first_timestamp": bucket_start, "last_timestamp": bucket_start}},
            "source": {"first_timestamp": bucket_start, "last_timestamp": bucket_start, "count": 1},
        }, sort_keys=True, separators=(",", ":")),
        complete=True,
    )


# --- P0-1: tombstone-only append -----------------------------------------------------------

def test_a_tombstone_only_append_invalidates_the_deleted_atoms_bucket(store):
    """The bucket that must be rebuilt is the one the DELETED atom was in.

    The first implementation read the last field of the prepared tombstone tuple, which is
    `thread_id`, not a timestamp. It was filtered out by an isinstance check, so a tombstone-only
    append recorded NOTHING and the contradicted bucket kept serving its pre-deletion total.
    """
    observed_at = 7_000.0
    store.append_batch(usage_atoms=[_usage("codex:t1:e1", observed_at)])
    baseline = _pending_rows(store)

    store.append_batch(usage_tombstones=[_tombstone("codex:t1:e1", observed_at)])

    recorded = {(r, b) for r, b, _g in (_pending_rows(store) - baseline)}
    assert recorded, "a tombstone-only append recorded no invalidation at all"
    expected_bucket = int(observed_at // RESOLUTION) * RESOLUTION
    assert (RESOLUTION, expected_bucket) in recorded, (
        f"the deleted atom's bucket {expected_bucket} was not invalidated; got {sorted(recorded)}"
    )


def test_a_tombstone_never_invalidates_a_bucket_derived_from_a_non_timestamp_field(store):
    """Red-capable control: a thread id must never be interpretable as an observed instant."""
    observed_at = 7_000.0
    store.append_batch(usage_atoms=[_usage("codex:99:e1", observed_at, thread_id="99")])
    baseline = _pending_rows(store)

    store.append_batch(usage_tombstones=[_tombstone("codex:99:e1", observed_at, thread_id="99")])

    recorded = {(r, b) for r, b, _g in (_pending_rows(store) - baseline)}
    # A numeric-looking thread id would land in bucket 0 if it were ever used as a timestamp.
    assert (RESOLUTION, 0) not in recorded, "a thread id was used as an observed instant"
    assert (RESOLUTION, int(observed_at // RESOLUTION) * RESOLUTION) in recorded


# --- P0-2: prune ---------------------------------------------------------------------------

def test_an_explicit_prune_invalidates_the_range_it_actually_deleted(store):
    """`Store.prune` advanced the generation and recorded no invalidation whatsoever."""
    old = 1_000.0
    store.append_batch(observations=[_observation("e-old", old)])
    baseline = _pending_rows(store)

    result = store.prune(now=old + storage.RETENTION_SECONDS + RESOLUTION)

    assert result.observations_deleted == 1, "the fixture did not actually prune anything"
    recorded = {(r, b) for r, b, _g in (_pending_rows(store) - baseline)}
    assert recorded, "an explicit prune deleted facts and invalidated no bucket"
    assert (RESOLUTION, int(old // RESOLUTION) * RESOLUTION) in recorded


def test_append_time_retention_prune_invalidates_the_deleted_range_not_the_offered_rows(store):
    """The pruned range is old; the offered rows are new. Recording the offered range is wrong."""
    old = 1_000.0
    store.append_batch(observations=[_observation("e-old", old)])
    baseline = _pending_rows(store)
    fresh = old + storage.RETENTION_SECONDS + 10 * RESOLUTION

    store.append_batch(
        observations=[_observation("e-new", fresh)],
        retention_now=fresh,
    )

    recorded = {(r, b) for r, b, _g in (_pending_rows(store) - baseline)}
    assert (RESOLUTION, int(old // RESOLUTION) * RESOLUTION) in recorded, (
        "the retention prune deleted an old fact without invalidating its bucket"
    )


# --- coverage and unavailable ---------------------------------------------------------------

def test_a_coverage_or_unavailable_change_invalidates_its_span(store):
    baseline = _pending(store)

    store.append_batch(
        coverage_epochs=[CoverageEpoch("cpu", "host", "epoch-c", 6_000.0, 6_300.0, 1.0, 1)],
        unavailable_spans=[UnavailableSpan("cpu", "host", "epoch-u", 6_600.0, 6_900.0, 1.0, "down", 1)],
    )

    recorded = _pending(store) - baseline
    for instant in (6_000.0, 6_300.0, 6_600.0, 6_900.0):
        assert (RESOLUTION, int(instant // RESOLUTION) * RESOLUTION) in recorded


# --- P0-3: retirement authority -------------------------------------------------------------

def test_a_publication_built_before_newer_facts_cannot_retire_their_invalidation(store):
    """Wall clock is not ordering authority; the source generation is.

    The real race: the materializer builds from a generation-N snapshot, facts then land and raise
    an invalidation at N+1, and only afterwards does the N publication reach the store with a LATER
    wall clock. Retiring by `created_at <= published_at` cleared that N+1 row, so a bucket the
    publication demonstrably cannot account for was marked reconciled and served.

    Publishing a strictly older generation is separately refused by the store
    (`source_generation cannot move backward`), which is why this uses an equal generation -- the
    narrowest form the store actually permits, and therefore the one that had to be caught here.
    """
    store.initialize_ring_storage()
    bucket_start = 7_140
    store.append_batch(observations=[_observation("e1", float(bucket_start) + 1.0)])
    first_generation = max(_pending_generations(store))
    store.publish_ring_buckets(
        buckets=[_bucket(bucket_start)], source_generation=first_generation, published_at=100.0,
    )
    assert (RESOLUTION, bucket_start) not in _pending(store)

    # Newer facts contradict the bucket again, at a newer generation.
    store.append_batch(observations=[_observation("e2", float(bucket_start) + 2.0)])
    newer_generation = max(_pending_generations(store))
    assert newer_generation > first_generation

    # The in-flight publication from the OLDER snapshot finally lands, with a later clock.
    store.publish_ring_buckets(
        buckets=[_bucket(bucket_start)], source_generation=first_generation, published_at=9_999_999.0,
    )

    assert (RESOLUTION, bucket_start) in _pending(store), (
        "a publication built before the contradicting facts retired their invalidation"
    )


# --- P0-4: cursor advancement ---------------------------------------------------------------

def test_same_horizon_publication_at_a_newer_generation_advances_the_cursor(store):
    """The cursor tracks folded GENERATION as well as horizon.

    Advancing only when the horizon moves left `folded_source_generation` stale for a republication
    of the same bucket at a newer generation -- which is exactly what a replay of contradicted work
    looks like.
    """
    store.initialize_ring_storage()
    bucket_start = 7_140
    store.publish_ring_buckets(
        buckets=[_bucket(bucket_start, value=1)], source_generation=3, published_at=100.0,
    )
    first = _cursor(store)[RESOLUTION]

    store.publish_ring_buckets(
        buckets=[_bucket(bucket_start, value=2)], source_generation=9, published_at=101.0,
    )
    second = _cursor(store)[RESOLUTION]

    assert second[0] == first[0], "the horizon did not move, and should not have"
    assert second[1] == 9, (
        f"folded_source_generation stayed {second[1]} after a newer-generation republication"
    )


def test_the_store_refuses_a_backward_publication_generation_outright(store):
    """Why the cursor cannot regress: the store rejects the publication before the cursor is asked.

    Recorded as its own row so the cursor rule above is not mistaken for last-writer-wins. The
    monotonicity is enforced upstream, and this fails loudly if that ever stops being true.
    """
    store.initialize_ring_storage()
    store.publish_ring_buckets(buckets=[_bucket(7_140)], source_generation=9, published_at=100.0)

    with pytest.raises(storage.StorageValidationError):
        store.publish_ring_buckets(buckets=[_bucket(7_140)], source_generation=3, published_at=101.0)

    assert _cursor(store)[RESOLUTION][1] == 9


# --- already-resolved properties that must not regress ---------------------------------------

def test_a_pending_invalidation_keeps_its_bucket_missing_from_a_served_window(store):
    store.initialize_ring_storage()
    bucket_start = 7_140
    store.publish_ring_buckets(
        buckets=[_bucket(bucket_start)], source_generation=0, published_at=100.0,
    )
    store.append_batch(observations=[_observation("e1", float(bucket_start) + 1.0)])

    window = store.read_ring_window(
        range_seconds=RANGE_SECONDS, resolution_seconds=RESOLUTION, window_end=7_200,
    )

    assert bucket_start in window.missing_bucket_starts
    assert all(row.bucket_start != bucket_start for row in window.rows)


def test_a_wrong_payload_version_keeps_its_bucket_missing(store):
    store.initialize_ring_storage()
    bucket_start = 7_140
    store.publish_ring_buckets(
        buckets=[_bucket(bucket_start)], source_generation=0, published_at=100.0,
    )
    connection = store._connection()
    connection.execute(
        "UPDATE aggregate_ring_slots SET payload_version = 99 WHERE bucket_start = ?",
        (bucket_start,),
    )
    connection.commit()

    window = store.read_ring_window(
        range_seconds=RANGE_SECONDS, resolution_seconds=RESOLUTION, window_end=7_200,
    )

    assert bucket_start in window.missing_bucket_starts


# --- P0-5: the ledger is the durable restart work owner ---------------------------------------

def test_a_restart_retains_an_invalidated_cell_even_when_its_slot_looks_persisted(tmp_path):
    """Exercised through `_restart_ring_cells` itself, not through the storage window.

    An earlier version of this row asserted only that `read_ring_window` exposed the pending
    bucket. That is a storage fact: disabling the service-side retention left it green, so it could
    not detect the defect it was written for. The filter is what drops work, so the filter is what
    has to be called.

    `_restart_ring_cells` exists to stop a restart's first build from synthesizing downtime as
    quiet zero, and it does that by DROPPING cells whose slots are already persisted. A bucket with
    an unapplied invalidation is persisted AND wrong, so dropping it left the contradicted payload
    in place with nothing left to rebuild it.
    """
    database = tmp_path / storage.DATABASE_FILENAME
    monotonic_now = [0.0]
    wall_now = [1_800_000_000.0]
    service = service_module.StatsCurrentService(
        tmp_path / "statsd.sock", database,
        monotonic=lambda: monotonic_now[0], clock=lambda: wall_now[0], randomizer=lambda: 0.0,
    )
    with storage.Store.open(database) as opened:
        service.writer = opened
        service._build_once(opened, True, frozenset())
        monotonic_now[0] = service_module.RING_FLUSH_SECONDS
        assert service._flush_ring_if_due() is not None

        candidate = service._cache.generation
        layer = next(item for item in candidate.layers if item.resolution == RESOLUTION)
        target = next(iter(sorted(bucket.start for bucket in layer.buckets)[-3:]))
        opened.append_batch(observations=[_observation("late", float(target) + 1.0)])
        assert (RESOLUTION, target) in _pending(opened), "the fixture recorded no invalidation"

        # A fresh restart: no publications yet in THIS process.
        service._ring_publications = 0
        cell = next(
            item for item in (
                materializer.DirtyCell(RESOLUTION, target),
            )
        )
        retained = service._restart_ring_cells(opened, candidate, frozenset({cell}))

    assert cell in retained, (
        "the restart filter dropped a cell the ledger had marked contradicted"
    )


def test_a_restart_still_drops_a_historically_open_cell(tmp_path):
    """Negative control: retaining everything would defeat the filter's original purpose.

    The filter's drop case is a cell whose persisted slot is INCOMPLETE and whose window closed
    before this process started -- a bucket that was open when the previous writer died. Rebuilding
    it would synthesize the downtime as quiet zero, which is the regression `_restart_ring_cells`
    exists to prevent. (A clean COMPLETE slot is deliberately retained, so it is not the control.)
    """
    database = tmp_path / storage.DATABASE_FILENAME
    monotonic_now = [0.0]
    wall_now = [1_800_000_000.0]
    service = service_module.StatsCurrentService(
        tmp_path / "statsd.sock", database,
        monotonic=lambda: monotonic_now[0], clock=lambda: wall_now[0], randomizer=lambda: 0.0,
    )
    with storage.Store.open(database) as opened:
        service.writer = opened
        service._build_once(opened, True, frozenset())
        monotonic_now[0] = service_module.RING_FLUSH_SECONDS
        assert service._flush_ring_if_due() is not None
        candidate = service._cache.generation
        layer = next(item for item in candidate.layers if item.resolution == RESOLUTION)
        historical = sorted(bucket.start for bucket in layer.buckets)[0]
        connection = opened._connection()
        connection.execute(
            "UPDATE aggregate_ring_slots SET complete = 0 "
            "WHERE resolution_seconds = ? AND bucket_start = ?",
            (RESOLUTION, historical),
        )
        connection.commit()
        assert not _pending(opened), "this control needs a store with no pending work"

        service._ring_publications = 0
        cell = materializer.DirtyCell(RESOLUTION, historical)
        retained = service._restart_ring_cells(opened, candidate, frozenset({cell}))

    assert cell not in retained, (
        "the restart filter retained a historically open cell; downtime will read as quiet zero"
    )


# --- bounded invalidation cardinality ---------------------------------------------------------

RING_SLOT_TOTAL = sum(storage.stats_resolution.RING_CAPACITIES.values())


@pytest.mark.parametrize("cutoff", (100_000.0, 1_700_000_000.0, 1_787_200_000.0))
def test_a_prune_range_is_bounded_by_the_ring_horizon_not_by_the_unix_epoch(cutoff):
    """The production OOM: `(0, cutoff)` walked from 1970.

    Measured before the clamp, at a current cutoff: 2,001,470,467 pairs across the four
    resolutions, roughly 134 GiB, materialized in ONE list inside the mutating transaction. Every
    pair past the horizon named a ring slot that cannot exist and could never be retired, so the
    cost bought nothing. Parametrized across three decades of cutoff because the defect scaled with
    the clock: a test at a single recent instant would not show that the bound is epoch-independent.
    """
    pairs = storage.invalidated_buckets((0.0, cutoff))

    assert len(pairs) <= RING_SLOT_TOTAL, (
        f"a prune at cutoff {cutoff} produced {len(pairs):,} pairs against a {RING_SLOT_TOTAL} "
        f"slot ceiling; the epoch walk is back"
    )
    assert len(pairs) == len(set(pairs)), "the bounded range emitted duplicates"


def test_the_bound_holds_for_every_configured_resolution_independently():
    """Per resolution, never more pairs than that ring has slots."""
    pairs = storage.invalidated_buckets((0.0, 1_787_200_000.0))
    per_resolution: dict[int, int] = {}
    for resolution_seconds, _bucket_start in pairs:
        per_resolution[resolution_seconds] = per_resolution.get(resolution_seconds, 0) + 1

    assert set(per_resolution) == set(storage.stats_resolution.RING_CAPACITIES)
    for resolution_seconds, count in per_resolution.items():
        assert count <= storage.stats_resolution.RING_CAPACITIES[resolution_seconds]


def test_an_ordinary_short_range_is_unchanged_by_the_clamp():
    """Negative control: clamping must not silently drop buckets a real mutation touches.

    A bound that returned nothing would satisfy every assertion above and destroy the ledger.
    """
    start = 1_787_200_000.0
    pairs = storage.invalidated_buckets((start, start + 120.0))

    by_resolution = {r: [b for r2, b in pairs if r2 == r] for r, _ in pairs}
    assert by_resolution[60], "a two-minute mutation invalidated no 60s bucket"
    assert len(by_resolution[60]) >= 2, "a two-minute span must cover at least two 60s buckets"
    assert all(len(v) >= 1 for v in by_resolution.values())


def test_an_explicit_prune_still_records_the_recent_buckets_it_deleted(store):
    """End-to-end control: the clamp must not stop a prune recording real work."""
    old = 1_787_000_000.0
    store.append_batch(observations=[_observation("e-old", old)])
    baseline = _pending_rows(store)

    result = store.prune(now=old + storage.RETENTION_SECONDS + RESOLUTION)

    assert result.observations_deleted == 1
    recorded = _pending_rows(store) - baseline
    assert recorded, "the clamped prune recorded nothing at all"
    assert len(recorded) <= RING_SLOT_TOTAL
