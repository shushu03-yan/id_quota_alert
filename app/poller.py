"""Single shared poller orchestration for the quota reliability core.

One poller serves every subscriber. A source failure is persisted as an observation and
never enters the confirmed-state engine. A successful non-duplicate snapshot is applied
atomically with confirmed state and quota events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import random
import sqlite3
import time
from typing import Callable

from .events import reconcile_snapshot
from .observations import ObservationOutcome
from .source import GovHKQuotaSourceAdapter
from .storage import (
    get_runtime_state,
    initialize_database,
    insert_quota_events,
    load_quota_states,
    record_observation,
    save_quota_states,
    set_runtime_state,
)


logger = logging.getLogger(__name__)
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class PollCycleResult:
    observed_at: datetime
    outcome: ObservationOutcome
    observation_id: int
    snapshot_applied: bool
    duplicate_snapshot: bool
    events_created: int
    backoff_required: bool
    error_code: str | None = None

    @property
    def successful(self) -> bool:
        return self.outcome is ObservationOutcome.SUCCESS


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _runtime_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("runtime datetime must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _runtime_datetime_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("runtime datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def calculate_poll_delay(
    *,
    interval_seconds: float,
    consecutive_failures: int,
    max_backoff_seconds: float,
    jitter_seconds: float,
    jitter_fraction: float,
) -> float:
    """Calculate bounded exponential backoff with positive jitter.

    A healthy poll uses the base interval. The first failed cycle waits twice the base
    interval, then 4x, 8x, etc., capped by ``max_backoff_seconds``. Jitter never makes
    the source polling frequency higher than the configured base/backoff interval.
    """

    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if consecutive_failures < 0:
        raise ValueError("consecutive_failures must be >= 0")
    if max_backoff_seconds < interval_seconds:
        raise ValueError("max_backoff_seconds must be >= interval_seconds")
    if jitter_seconds < 0:
        raise ValueError("jitter_seconds must be >= 0")
    if not 0 <= jitter_fraction <= 1:
        raise ValueError("jitter_fraction must be between 0 and 1")

    if consecutive_failures == 0:
        base = interval_seconds
    else:
        exponent = min(consecutive_failures, 20)
        base = min(max_backoff_seconds, interval_seconds * (2**exponent))
    return base + jitter_seconds * jitter_fraction


class QuotaPoller:
    """Run one or more safe polling cycles against one shared source adapter."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        source: GovHKQuotaSourceAdapter,
        *,
        missing_confirmations_required: int = 2,
        clock: Clock = _utc_now,
    ) -> None:
        if missing_confirmations_required < 1:
            raise ValueError("missing_confirmations_required must be >= 1")
        self.connection = connection
        self.source = source
        self.missing_confirmations_required = missing_confirmations_required
        self.clock = clock
        initialize_database(connection)

    def run_once(self) -> PollCycleResult:
        observed_at = self.clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("poller clock must return a timezone-aware datetime")

        previous_source_updated_at = _runtime_datetime(
            get_runtime_state(self.connection, "last_source_updated_at")
        )
        previous_payload_hash = get_runtime_state(self.connection, "last_payload_hash")
        source_result = self.source.read(
            observed_at=observed_at,
            previous_source_updated_at=previous_source_updated_at,
            previous_payload_hash=previous_payload_hash,
        )

        with self.connection:
            observation_id = record_observation(
                self.connection,
                source_result.observation,
            )
            set_runtime_state(
                self.connection,
                "last_poll_attempt",
                _runtime_datetime_text(observed_at),
                updated_at=observed_at,
            )
            set_runtime_state(
                self.connection,
                "last_poll_outcome",
                source_result.observation.outcome.value,
                updated_at=observed_at,
            )
            if source_result.well_formed:
                set_runtime_state(
                    self.connection,
                    "last_well_formed_poll",
                    _runtime_datetime_text(observed_at),
                    updated_at=observed_at,
                )
            if source_result.observation.source_updated_at is not None:
                set_runtime_state(
                    self.connection,
                    "last_received_source_updated_at",
                    _runtime_datetime_text(source_result.observation.source_updated_at),
                    updated_at=observed_at,
                )
            if source_result.observation.outcome is ObservationOutcome.STALE:
                set_runtime_state(
                    self.connection,
                    "last_stale_snapshot",
                    _runtime_datetime_text(observed_at),
                    updated_at=observed_at,
                )
                if previous_source_updated_at is not None:
                    received_at = source_result.observation.source_updated_at
                    assert received_at is not None
                    regression_seconds = (
                        previous_source_updated_at - received_at
                    ).total_seconds()
                    set_runtime_state(
                        self.connection,
                        "last_source_regression_seconds",
                        f"{regression_seconds:.1f}",
                        updated_at=observed_at,
                    )
            elif source_result.observation.error_code == "source_version_conflict":
                set_runtime_state(
                    self.connection,
                    "last_source_version_conflict",
                    _runtime_datetime_text(observed_at),
                    updated_at=observed_at,
                )

            if not source_result.successful:
                return PollCycleResult(
                    observed_at=observed_at,
                    outcome=source_result.observation.outcome,
                    observation_id=observation_id,
                    snapshot_applied=False,
                    duplicate_snapshot=False,
                    events_created=0,
                    backoff_required=source_result.should_backoff,
                    error_code=source_result.observation.error_code,
                )

            snapshot = source_result.snapshot
            assert snapshot is not None

            last_payload_hash = get_runtime_state(self.connection, "last_payload_hash")
            duplicate_snapshot = last_payload_hash == snapshot.payload_hash

            set_runtime_state(
                self.connection,
                "last_successful_poll",
                _runtime_datetime_text(observed_at),
                updated_at=observed_at,
            )
            set_runtime_state(
                self.connection,
                "last_valid_snapshot",
                _runtime_datetime_text(snapshot.observed_at),
                updated_at=observed_at,
            )
            set_runtime_state(
                self.connection,
                "last_payload_hash",
                snapshot.payload_hash,
                updated_at=observed_at,
            )
            if snapshot.source_updated_at is not None:
                set_runtime_state(
                    self.connection,
                    "last_source_updated_at",
                    _runtime_datetime_text(snapshot.source_updated_at),
                    updated_at=observed_at,
                )

            if duplicate_snapshot:
                return PollCycleResult(
                    observed_at=observed_at,
                    outcome=ObservationOutcome.SUCCESS,
                    observation_id=observation_id,
                    snapshot_applied=False,
                    duplicate_snapshot=True,
                    events_created=0,
                    backoff_required=False,
                )

            previous_states = load_quota_states(self.connection)
            is_initial_baseline = (
                get_runtime_state(self.connection, "baseline_initialized") != "1"
            )
            reconciled = reconcile_snapshot(
                previous_states,
                snapshot,
                is_initial_baseline=is_initial_baseline,
                missing_confirmations_required=self.missing_confirmations_required,
            )
            save_quota_states(self.connection, reconciled.states.values())
            event_ids = insert_quota_events(
                self.connection,
                reconciled.events,
                created_at=observed_at,
            )
            set_runtime_state(
                self.connection,
                "baseline_initialized",
                "1",
                updated_at=observed_at,
            )

            return PollCycleResult(
                observed_at=observed_at,
                outcome=ObservationOutcome.SUCCESS,
                observation_id=observation_id,
                snapshot_applied=True,
                duplicate_snapshot=False,
                events_created=len(event_ids),
                backoff_required=False,
            )

    def run_forever(
        self,
        *,
        interval_seconds: float = 60.0,
        jitter_seconds: float = 5.0,
        max_backoff_seconds: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
        random_fraction: Callable[[], float] = random.random,
    ) -> None:
        """Continuously poll with bounded backoff and positive jitter."""

        consecutive_failures = 0
        while True:
            result = self.run_once()
            if result.backoff_required:
                consecutive_failures += 1
            else:
                consecutive_failures = 0

            delay = calculate_poll_delay(
                interval_seconds=interval_seconds,
                consecutive_failures=consecutive_failures,
                max_backoff_seconds=max_backoff_seconds,
                jitter_seconds=jitter_seconds,
                jitter_fraction=random_fraction(),
            )
            logger.info(
                "quota poll outcome=%s applied=%s duplicate=%s events=%d backoff=%s error=%s next_poll_in=%.1fs",
                result.outcome.value,
                result.snapshot_applied,
                result.duplicate_snapshot,
                result.events_created,
                result.backoff_required,
                result.error_code or "-",
                delay,
            )
            sleep(delay)
