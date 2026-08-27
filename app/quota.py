"""Normalized quota domain types and snapshot validation.

This module deliberately does not know how GovHK is fetched or parsed. The future
adapter must first normalize source data into these types and pass validation before
state is allowed to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Iterable


class QuotaStatus(StrEnum):
    UNAVAILABLE = "unavailable"
    LIMITED = "limited"
    AVAILABLE = "available"

    @property
    def rank(self) -> int:
        return {
            QuotaStatus.UNAVAILABLE: 0,
            QuotaStatus.LIMITED: 1,
            QuotaStatus.AVAILABLE: 2,
        }[self]


@dataclass(frozen=True, slots=True, order=True)
class QuotaKey:
    date: date
    office_id: str


@dataclass(frozen=True, slots=True)
class QuotaEntry:
    key: QuotaKey
    status: QuotaStatus
    service_periods: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidatedSnapshot:
    observed_at: datetime
    source_updated_at: datetime | None
    payload_hash: str
    parser_version: str
    entries: tuple[QuotaEntry, ...]

    def by_key(self) -> dict[QuotaKey, QuotaEntry]:
        return {entry.key: entry for entry in self.entries}


class SnapshotValidationError(ValueError):
    """Raised when a source snapshot is unsafe to apply to confirmed state."""


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SnapshotValidationError(f"{field_name} must be timezone-aware")


def validate_snapshot(
    entries: Iterable[QuotaEntry],
    *,
    observed_at: datetime,
    source_updated_at: datetime | None,
    payload_hash: str,
    parser_version: str,
    expected_keys: set[QuotaKey] | frozenset[QuotaKey] | None = None,
    minimum_entries: int = 1,
) -> ValidatedSnapshot:
    """Validate a normalized source snapshot before it may drive state changes.

    `expected_keys` is intentionally supplied by the source adapter. When the page or
    endpoint has a known expected coverage, a partial response is rejected instead of
    being mistaken for quota disappearance.
    """

    _require_aware(observed_at, "observed_at")
    if source_updated_at is not None:
        _require_aware(source_updated_at, "source_updated_at")

    if not payload_hash.strip():
        raise SnapshotValidationError("payload_hash must not be empty")
    if not parser_version.strip():
        raise SnapshotValidationError("parser_version must not be empty")
    if minimum_entries < 0:
        raise SnapshotValidationError("minimum_entries must be >= 0")

    normalized = tuple(entries)
    if len(normalized) < minimum_entries:
        raise SnapshotValidationError(
            f"snapshot contains {len(normalized)} entries; expected at least {minimum_entries}"
        )

    seen: set[QuotaKey] = set()
    for entry in normalized:
        if not entry.key.office_id.strip():
            raise SnapshotValidationError("office_id must not be empty")
        if entry.key in seen:
            raise SnapshotValidationError(f"duplicate quota key: {entry.key}")
        seen.add(entry.key)

    if expected_keys is not None:
        missing = set(expected_keys) - seen
        if missing:
            preview = ", ".join(str(key) for key in sorted(missing)[:3])
            raise SnapshotValidationError(
                f"snapshot is incomplete; {len(missing)} expected keys are missing: {preview}"
            )

    return ValidatedSnapshot(
        observed_at=observed_at,
        source_updated_at=source_updated_at,
        payload_hash=payload_hash,
        parser_version=parser_version,
        entries=normalized,
    )
