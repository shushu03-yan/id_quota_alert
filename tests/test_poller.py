from datetime import date, datetime, timedelta, timezone

import pytest

from app.observations import ObservationOutcome, QuotaObservation
from app.poller import QuotaPoller, calculate_poll_delay
from app.quota import QuotaEntry, QuotaKey, QuotaStatus, validate_snapshot
from app.source import SourceReadResult
from app.storage import connect_database, get_runtime_state, load_quota_states


BASE_TIME = datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc)
KEY = QuotaKey(date(2026, 9, 1), "RHK")


def success_result(
    status: QuotaStatus,
    *,
    minute: int,
    payload_hash: str,
    service_periods=(),
) -> SourceReadResult:
    observed_at = BASE_TIME + timedelta(minutes=minute)
    source_updated_at = observed_at - timedelta(seconds=30)
    snapshot = validate_snapshot(
        [QuotaEntry(KEY, status, tuple(service_periods))],
        observed_at=observed_at,
        source_updated_at=source_updated_at,
        payload_hash=payload_hash,
        parser_version="test-source-v1",
    )
    observation = QuotaObservation(
        observed_at=observed_at,
        outcome=ObservationOutcome.SUCCESS,
        source_updated_at=source_updated_at,
        payload_hash=payload_hash,
        parser_version="test-source-v1",
        office_count=1,
        quota_count=1,
    )
    return SourceReadResult(observation=observation, snapshot=snapshot)


def failure_result(*, minute: int, error_code="http_429") -> SourceReadResult:
    observed_at = BASE_TIME + timedelta(minutes=minute)
    return SourceReadResult(
        observation=QuotaObservation(
            observed_at=observed_at,
            outcome=ObservationOutcome.FETCH_ERROR,
            error_code=error_code,
        ),
        snapshot=None,
    )


def stale_result(*, minute: int, payload_hash: str, source_updated_at: datetime) -> SourceReadResult:
    observed_at = BASE_TIME + timedelta(minutes=minute)
    return SourceReadResult(
        observation=QuotaObservation(
            observed_at=observed_at,
            outcome=ObservationOutcome.STALE,
            source_updated_at=source_updated_at,
            payload_hash=payload_hash,
            parser_version="test-source-v1",
            office_count=1,
            quota_count=1,
            error_code="source_time_regression",
        ),
        snapshot=None,
    )


class SequenceSource:
    def __init__(self, *results: SourceReadResult):
        self.results = list(results)
        self.previous_source_updated_at = []
        self.previous_payload_hash = []

    def read(
        self,
        *,
        observed_at,
        previous_source_updated_at=None,
        previous_payload_hash=None,
    ):
        self.previous_source_updated_at.append(previous_source_updated_at)
        self.previous_payload_hash.append(previous_payload_hash)
        result = self.results.pop(0)
        # The poller owns the clock. Test fixtures use matching timestamps so an
        # observation written by the fake remains internally consistent.
        assert result.observation.observed_at == observed_at
        return result


def clock_sequence(*moments):
    values = iter(moments)
    return lambda: next(values)


def test_first_successful_snapshot_builds_baseline_without_event(tmp_path) -> None:
    source = SequenceSource(
        success_result(
            QuotaStatus.AVAILABLE,
            minute=0,
            payload_hash="hash-1",
            service_periods=("R",),
        )
    )
    connection = connect_database(tmp_path / "poller.sqlite3")
    poller = QuotaPoller(connection, source, clock=lambda: BASE_TIME)

    cycle = poller.run_once()

    assert cycle.successful is True
    assert cycle.snapshot_applied is True
    assert cycle.events_created == 0
    assert get_runtime_state(connection, "baseline_initialized") == "1"
    states = load_quota_states(connection)
    assert states[KEY].status is QuotaStatus.AVAILABLE
    assert states[KEY].occurrence_id is not None
    assert states[KEY].service_periods == ("R",)
    assert connection.execute("SELECT COUNT(*) FROM quota_events").fetchone()[0] == 0


def test_second_snapshot_creates_event_and_survives_poller_restart(tmp_path) -> None:
    first = success_result(
        QuotaStatus.UNAVAILABLE,
        minute=0,
        payload_hash="hash-1",
    )
    second = success_result(
        QuotaStatus.LIMITED,
        minute=1,
        payload_hash="hash-2",
        service_periods=("K",),
    )
    connection = connect_database(tmp_path / "poller.sqlite3")

    QuotaPoller(
        connection,
        SequenceSource(first),
        clock=lambda: BASE_TIME,
    ).run_once()

    restarted_source = SequenceSource(second)
    restarted = QuotaPoller(
        connection,
        restarted_source,
        clock=lambda: BASE_TIME + timedelta(minutes=1),
    )
    cycle = restarted.run_once()

    assert cycle.events_created == 1
    event = connection.execute(
        "SELECT from_status, to_status, occurrence_id FROM quota_events"
    ).fetchone()
    assert event["from_status"] == "unavailable"
    assert event["to_status"] == "limited"
    assert event["occurrence_id"]
    states = load_quota_states(connection)
    assert states[KEY].status is QuotaStatus.LIMITED
    assert states[KEY].service_periods == ("K",)
    assert restarted_source.previous_source_updated_at == [first.snapshot.source_updated_at]
    assert restarted_source.previous_payload_hash == ["hash-1"]


def test_failed_observation_never_changes_confirmed_state(tmp_path) -> None:
    baseline = success_result(
        QuotaStatus.AVAILABLE,
        minute=0,
        payload_hash="hash-1",
        service_periods=("R",),
    )
    failure = failure_result(minute=1)
    source = SequenceSource(baseline, failure)
    connection = connect_database(tmp_path / "poller.sqlite3")
    poller = QuotaPoller(
        connection,
        source,
        clock=clock_sequence(BASE_TIME, BASE_TIME + timedelta(minutes=1)),
    )

    poller.run_once()
    before = load_quota_states(connection)[KEY]
    failed_cycle = poller.run_once()
    after = load_quota_states(connection)[KEY]

    assert failed_cycle.successful is False
    assert failed_cycle.error_code == "http_429"
    assert before == after
    observations = connection.execute(
        "SELECT outcome, error_code FROM quota_observations ORDER BY id"
    ).fetchall()
    assert [(row["outcome"], row["error_code"]) for row in observations] == [
        ("success", None),
        ("fetch_error", "http_429"),
    ]
    assert get_runtime_state(connection, "last_successful_poll") == BASE_TIME.isoformat().replace(
        "+00:00", "Z"
    )


def test_duplicate_payload_is_recorded_but_not_reapplied(tmp_path) -> None:
    first = success_result(
        QuotaStatus.LIMITED,
        minute=0,
        payload_hash="same-hash",
        service_periods=("R",),
    )
    duplicate = success_result(
        QuotaStatus.LIMITED,
        minute=1,
        payload_hash="same-hash",
        service_periods=("R",),
    )
    source = SequenceSource(first, duplicate)
    connection = connect_database(tmp_path / "poller.sqlite3")
    poller = QuotaPoller(
        connection,
        source,
        clock=clock_sequence(BASE_TIME, BASE_TIME + timedelta(minutes=1)),
    )

    poller.run_once()
    before = load_quota_states(connection)[KEY]
    cycle = poller.run_once()
    after = load_quota_states(connection)[KEY]

    assert cycle.successful is True
    assert cycle.duplicate_snapshot is True
    assert cycle.snapshot_applied is False
    assert cycle.events_created == 0
    assert before == after
    assert connection.execute("SELECT COUNT(*) FROM quota_observations").fetchone()[0] == 2


def test_stale_snapshot_is_audited_without_state_change_or_backoff(tmp_path) -> None:
    baseline = success_result(
        QuotaStatus.AVAILABLE,
        minute=0,
        payload_hash="newer-hash",
        service_periods=("R",),
    )
    older_source_time = baseline.snapshot.source_updated_at - timedelta(seconds=32)
    stale = stale_result(
        minute=1,
        payload_hash="older-hash",
        source_updated_at=older_source_time,
    )
    connection = connect_database(tmp_path / "poller.sqlite3")
    poller = QuotaPoller(
        connection,
        SequenceSource(baseline, stale),
        clock=clock_sequence(BASE_TIME, BASE_TIME + timedelta(minutes=1)),
    )

    poller.run_once()
    before = load_quota_states(connection)[KEY]
    cycle = poller.run_once()
    after = load_quota_states(connection)[KEY]

    assert cycle.successful is False
    assert cycle.backoff_required is False
    assert cycle.outcome is ObservationOutcome.STALE
    assert cycle.events_created == 0
    assert before == after
    assert get_runtime_state(connection, "last_source_updated_at") == (
        baseline.snapshot.source_updated_at.isoformat().replace("+00:00", "Z")
    )
    assert get_runtime_state(connection, "last_received_source_updated_at") == (
        older_source_time.isoformat().replace("+00:00", "Z")
    )
    assert get_runtime_state(connection, "last_source_regression_seconds") == "32.0"
    row = connection.execute(
        "SELECT outcome, source_updated_at, office_count, quota_count, error_code "
        "FROM quota_observations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert tuple(row) == (
        "stale",
        older_source_time.isoformat().replace("+00:00", "Z"),
        1,
        1,
        "source_time_regression",
    )


def test_stale_snapshot_keeps_base_delay_while_real_failure_backs_off(tmp_path) -> None:
    baseline = success_result(
        QuotaStatus.UNAVAILABLE,
        minute=0,
        payload_hash="baseline-hash",
    )
    stale = stale_result(
        minute=1,
        payload_hash="stale-hash",
        source_updated_at=baseline.snapshot.source_updated_at - timedelta(seconds=5),
    )
    failure = failure_result(minute=2, error_code="timeout")
    poller = QuotaPoller(
        connect_database(tmp_path / "poller.sqlite3"),
        SequenceSource(baseline, stale, failure),
        clock=clock_sequence(
            BASE_TIME,
            BASE_TIME + timedelta(minutes=1),
            BASE_TIME + timedelta(minutes=2),
        ),
    )
    delays = []

    def stop_after_three_cycles(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise StopIteration

    with pytest.raises(StopIteration):
        poller.run_forever(
            interval_seconds=60,
            jitter_seconds=0,
            max_backoff_seconds=900,
            sleep=stop_after_three_cycles,
            random_fraction=lambda: 0,
        )

    assert delays == [60, 60, 120]


def test_status_upgrade_is_persisted_once(tmp_path) -> None:
    baseline = success_result(
        QuotaStatus.LIMITED,
        minute=0,
        payload_hash="hash-1",
        service_periods=("R",),
    )
    upgrade = success_result(
        QuotaStatus.AVAILABLE,
        minute=1,
        payload_hash="hash-2",
        service_periods=("R", "K"),
    )
    duplicate_upgrade = success_result(
        QuotaStatus.AVAILABLE,
        minute=2,
        payload_hash="hash-2",
        service_periods=("R", "K"),
    )
    source = SequenceSource(baseline, upgrade, duplicate_upgrade)
    connection = connect_database(tmp_path / "poller.sqlite3")
    poller = QuotaPoller(
        connection,
        source,
        clock=clock_sequence(
            BASE_TIME,
            BASE_TIME + timedelta(minutes=1),
            BASE_TIME + timedelta(minutes=2),
        ),
    )

    poller.run_once()
    upgraded = poller.run_once()
    duplicate = poller.run_once()

    assert upgraded.events_created == 1
    assert duplicate.events_created == 0
    assert duplicate.duplicate_snapshot is True
    rows = connection.execute(
        "SELECT from_status, to_status FROM quota_events"
    ).fetchall()
    assert [(row["from_status"], row["to_status"]) for row in rows] == [
        ("limited", "available")
    ]


def test_calculate_poll_delay_uses_positive_jitter_and_bounded_backoff() -> None:
    assert calculate_poll_delay(
        interval_seconds=60,
        consecutive_failures=0,
        max_backoff_seconds=900,
        jitter_seconds=10,
        jitter_fraction=0.5,
    ) == 65
    assert calculate_poll_delay(
        interval_seconds=60,
        consecutive_failures=1,
        max_backoff_seconds=900,
        jitter_seconds=10,
        jitter_fraction=0.5,
    ) == 125
    assert calculate_poll_delay(
        interval_seconds=60,
        consecutive_failures=20,
        max_backoff_seconds=900,
        jitter_seconds=10,
        jitter_fraction=1,
    ) == 910
