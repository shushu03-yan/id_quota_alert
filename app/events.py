"""Pure confirmed-state and quota-event reconciliation logic.

Only a `ValidatedSnapshot` may enter this module. Missing entries are confirmed across
multiple successful snapshots before an active occurrence is closed, reducing false
alerts caused by partial or transient source responses.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Mapping
from uuid import uuid4

from .quota import QuotaEntry, QuotaKey, QuotaStatus, ValidatedSnapshot


@dataclass(frozen=True, slots=True)
class QuotaState:
    key: QuotaKey
    status: QuotaStatus
    occurrence_id: str | None
    first_observed_at: datetime
    last_observed_at: datetime
    source_updated_at: datetime | None
    missing_count: int = 0


@dataclass(frozen=True, slots=True)
class QuotaEvent:
    key: QuotaKey
    from_status: QuotaStatus
    to_status: QuotaStatus
    occurrence_id: str
    observed_at: datetime
    source_updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    states: dict[QuotaKey, QuotaState]
    events: tuple[QuotaEvent, ...]


def _new_occurrence_id() -> str:
    return uuid4().hex


def _state_from_entry(
    entry: QuotaEntry,
    snapshot: ValidatedSnapshot,
    occurrence_id: str | None,
) -> QuotaState:
    return QuotaState(
        key=entry.key,
        status=entry.status,
        occurrence_id=occurrence_id,
        first_observed_at=snapshot.observed_at,
        last_observed_at=snapshot.observed_at,
        source_updated_at=snapshot.source_updated_at,
        missing_count=0,
    )


def reconcile_snapshot(
    previous_states: Mapping[QuotaKey, QuotaState],
    snapshot: ValidatedSnapshot,
    *,
    is_initial_baseline: bool = False,
    missing_confirmations_required: int = 2,
    occurrence_factory: Callable[[], str] | None = None,
) -> ReconcileResult:
    """Apply one validated snapshot to confirmed state and emit notify-worthy events.

    The first production snapshot should be processed with `is_initial_baseline=True`.
    Existing availability is then recorded without sending historical alerts. Later
    appearances and status upgrades produce events.
    """

    if missing_confirmations_required < 1:
        raise ValueError("missing_confirmations_required must be >= 1")

    occurrence_factory = occurrence_factory or _new_occurrence_id
    current = snapshot.by_key()
    next_states: dict[QuotaKey, QuotaState] = {}
    events: list[QuotaEvent] = []

    for key, entry in current.items():
        previous = previous_states.get(key)

        if entry.status is QuotaStatus.UNAVAILABLE:
            if previous is not None and previous.status is QuotaStatus.UNAVAILABLE:
                next_states[key] = replace(
                    previous,
                    last_observed_at=snapshot.observed_at,
                    source_updated_at=snapshot.source_updated_at,
                    missing_count=0,
                )
            else:
                next_states[key] = _state_from_entry(entry, snapshot, None)
            continue

        if previous is None or previous.status is QuotaStatus.UNAVAILABLE:
            occurrence_id = occurrence_factory()
            next_states[key] = _state_from_entry(entry, snapshot, occurrence_id)
            if not is_initial_baseline:
                events.append(
                    QuotaEvent(
                        key=key,
                        from_status=QuotaStatus.UNAVAILABLE,
                        to_status=entry.status,
                        occurrence_id=occurrence_id,
                        observed_at=snapshot.observed_at,
                        source_updated_at=snapshot.source_updated_at,
                    )
                )
            continue

        occurrence_id = previous.occurrence_id or occurrence_factory()
        next_states[key] = QuotaState(
            key=key,
            status=entry.status,
            occurrence_id=occurrence_id,
            first_observed_at=previous.first_observed_at,
            last_observed_at=snapshot.observed_at,
            source_updated_at=snapshot.source_updated_at,
            missing_count=0,
        )

        if entry.status.rank > previous.status.rank:
            events.append(
                QuotaEvent(
                    key=key,
                    from_status=previous.status,
                    to_status=entry.status,
                    occurrence_id=occurrence_id,
                    observed_at=snapshot.observed_at,
                    source_updated_at=snapshot.source_updated_at,
                )
            )

    for key, previous in previous_states.items():
        if key in current:
            continue

        if previous.status is QuotaStatus.UNAVAILABLE:
            next_states[key] = previous
            continue

        missing_count = previous.missing_count + 1
        if missing_count >= missing_confirmations_required:
            next_states[key] = QuotaState(
                key=key,
                status=QuotaStatus.UNAVAILABLE,
                occurrence_id=None,
                first_observed_at=snapshot.observed_at,
                last_observed_at=previous.last_observed_at,
                source_updated_at=previous.source_updated_at,
                missing_count=0,
            )
        else:
            next_states[key] = replace(previous, missing_count=missing_count)

    return ReconcileResult(states=next_states, events=tuple(events))
