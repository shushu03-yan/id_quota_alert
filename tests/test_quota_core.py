from datetime import date, datetime, timedelta, timezone

import pytest

from app.events import reconcile_snapshot
from app.quota import (
    QuotaEntry,
    QuotaKey,
    QuotaStatus,
    SnapshotValidationError,
    validate_snapshot,
)


BASE_TIME = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
KEY_A = QuotaKey(date(2026, 9, 1), "office-a")
KEY_B = QuotaKey(date(2026, 9, 1), "office-b")


def snapshot(*entries: QuotaEntry, minute: int = 0):
    return validate_snapshot(
        entries,
        observed_at=BASE_TIME + timedelta(minutes=minute),
        source_updated_at=BASE_TIME + timedelta(minutes=minute),
        payload_hash=f"payload-{minute}",
        parser_version="test-v1",
    )


def test_partial_snapshot_is_rejected_when_expected_keys_are_known() -> None:
    with pytest.raises(SnapshotValidationError, match="incomplete"):
        validate_snapshot(
            [QuotaEntry(KEY_A, QuotaStatus.AVAILABLE)],
            observed_at=BASE_TIME,
            source_updated_at=BASE_TIME,
            payload_hash="hash",
            parser_version="test-v1",
            expected_keys={KEY_A, KEY_B},
        )


def test_initial_baseline_records_availability_without_historical_event() -> None:
    result = reconcile_snapshot(
        {},
        snapshot(QuotaEntry(KEY_A, QuotaStatus.AVAILABLE)),
        is_initial_baseline=True,
        occurrence_factory=lambda: "occ-1",
    )

    assert result.events == ()
    assert result.states[KEY_A].status is QuotaStatus.AVAILABLE
    assert result.states[KEY_A].occurrence_id == "occ-1"


def test_one_missing_snapshot_does_not_close_active_occurrence() -> None:
    baseline = reconcile_snapshot(
        {},
        snapshot(QuotaEntry(KEY_A, QuotaStatus.AVAILABLE)),
        is_initial_baseline=True,
        occurrence_factory=lambda: "occ-1",
    )

    result = reconcile_snapshot(
        baseline.states,
        snapshot(QuotaEntry(KEY_B, QuotaStatus.UNAVAILABLE), minute=1),
        missing_confirmations_required=2,
    )

    assert result.states[KEY_A].status is QuotaStatus.AVAILABLE
    assert result.states[KEY_A].occurrence_id == "occ-1"
    assert result.states[KEY_A].missing_count == 1
    assert result.events == ()


def test_confirmed_disappearance_then_reappearance_creates_new_occurrence() -> None:
    ids = iter(["occ-1", "occ-2"])
    baseline = reconcile_snapshot(
        {},
        snapshot(QuotaEntry(KEY_A, QuotaStatus.LIMITED)),
        is_initial_baseline=True,
        occurrence_factory=ids.__next__,
    )

    missing_once = reconcile_snapshot(
        baseline.states,
        snapshot(QuotaEntry(KEY_B, QuotaStatus.UNAVAILABLE), minute=1),
        missing_confirmations_required=2,
        occurrence_factory=ids.__next__,
    )
    missing_twice = reconcile_snapshot(
        missing_once.states,
        snapshot(QuotaEntry(KEY_B, QuotaStatus.UNAVAILABLE), minute=2),
        missing_confirmations_required=2,
        occurrence_factory=ids.__next__,
    )

    assert missing_twice.states[KEY_A].status is QuotaStatus.UNAVAILABLE
    assert missing_twice.states[KEY_A].occurrence_id is None

    reappeared = reconcile_snapshot(
        missing_twice.states,
        snapshot(
            QuotaEntry(KEY_A, QuotaStatus.LIMITED),
            QuotaEntry(KEY_B, QuotaStatus.UNAVAILABLE),
            minute=3,
        ),
        occurrence_factory=ids.__next__,
    )

    assert len(reappeared.events) == 1
    assert reappeared.events[0].from_status is QuotaStatus.UNAVAILABLE
    assert reappeared.events[0].to_status is QuotaStatus.LIMITED
    assert reappeared.events[0].occurrence_id == "occ-2"


def test_limited_to_available_is_an_upgrade_event_in_same_occurrence() -> None:
    baseline = reconcile_snapshot(
        {},
        snapshot(QuotaEntry(KEY_A, QuotaStatus.LIMITED)),
        is_initial_baseline=True,
        occurrence_factory=lambda: "occ-1",
    )

    upgraded = reconcile_snapshot(
        baseline.states,
        snapshot(QuotaEntry(KEY_A, QuotaStatus.AVAILABLE), minute=1),
    )

    assert len(upgraded.events) == 1
    assert upgraded.events[0].from_status is QuotaStatus.LIMITED
    assert upgraded.events[0].to_status is QuotaStatus.AVAILABLE
    assert upgraded.events[0].occurrence_id == "occ-1"


def test_available_to_limited_updates_state_without_alert_event() -> None:
    baseline = reconcile_snapshot(
        {},
        snapshot(QuotaEntry(KEY_A, QuotaStatus.AVAILABLE)),
        is_initial_baseline=True,
        occurrence_factory=lambda: "occ-1",
    )

    downgraded = reconcile_snapshot(
        baseline.states,
        snapshot(QuotaEntry(KEY_A, QuotaStatus.LIMITED), minute=1),
    )

    assert downgraded.states[KEY_A].status is QuotaStatus.LIMITED
    assert downgraded.events == ()
