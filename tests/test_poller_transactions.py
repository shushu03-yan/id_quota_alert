from datetime import date, datetime, timezone
import sqlite3

import pytest

import app.poller as poller_module
from app.events import QuotaEvent
from app.observations import ObservationOutcome, QuotaObservation
from app.poller import QuotaPoller
from app.quota import QuotaEntry, QuotaKey, QuotaStatus, validate_snapshot
from app.source import SourceReadResult
from app.storage import connect_database, initialize_database, insert_quota_events


NOW = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)
KEY = QuotaKey(date(2026, 9, 1), "RHK")


class OneShotSuccessfulSource:
    def read(
        self,
        *,
        observed_at,
        previous_source_updated_at=None,
        previous_payload_hash=None,
    ):
        snapshot = validate_snapshot(
            [QuotaEntry(KEY, QuotaStatus.AVAILABLE, ("R",))],
            observed_at=observed_at,
            source_updated_at=observed_at,
            payload_hash="transaction-test-hash",
            parser_version="test-source-v1",
        )
        return SourceReadResult(
            observation=QuotaObservation(
                observed_at=observed_at,
                outcome=ObservationOutcome.SUCCESS,
                source_updated_at=observed_at,
                payload_hash=snapshot.payload_hash,
                parser_version=snapshot.parser_version,
                office_count=1,
                quota_count=1,
            ),
            snapshot=snapshot,
        )


def test_successful_snapshot_writes_roll_back_together_on_persistence_failure(
    tmp_path, monkeypatch
) -> None:
    connection = connect_database(tmp_path / "atomic.sqlite3")
    poller = QuotaPoller(
        connection,
        OneShotSuccessfulSource(),
        clock=lambda: NOW,
    )

    def fail_event_insert(*args, **kwargs):
        raise RuntimeError("simulated event persistence failure")

    monkeypatch.setattr(poller_module, "insert_quota_events", fail_event_insert)

    with pytest.raises(RuntimeError, match="simulated event persistence failure"):
        poller.run_once()

    assert connection.execute("SELECT COUNT(*) FROM quota_observations").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM quota_state").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM quota_events").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM runtime_state").fetchone()[0] == 0


def test_event_dedup_does_not_hide_other_database_constraint_errors(tmp_path) -> None:
    connection = connect_database(tmp_path / "constraints.sqlite3")
    initialize_database(connection)
    invalid_event = QuotaEvent(
        key=KEY,
        from_status=QuotaStatus.AVAILABLE,
        # QuotaEvent's runtime dataclass does not enforce the annotation; this deliberately
        # violates the SQLite CHECK so the persistence layer must surface the error.
        to_status=QuotaStatus.UNAVAILABLE,
        occurrence_id="occ-invalid",
        observed_at=NOW,
        source_updated_at=NOW,
    )

    with pytest.raises(sqlite3.IntegrityError):
        with connection:
            insert_quota_events(connection, [invalid_event], created_at=NOW)
