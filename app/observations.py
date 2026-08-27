"""Audit records for every quota source observation.

Fetch/parse failures are observations, not quota states. Keeping that distinction is
critical: a timeout or malformed payload must never be interpreted as 'unavailable'.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ObservationOutcome(StrEnum):
    SUCCESS = "success"
    FETCH_ERROR = "fetch_error"
    PARSE_ERROR = "parse_error"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class QuotaObservation:
    observed_at: datetime
    outcome: ObservationOutcome
    source_updated_at: datetime | None = None
    payload_hash: str | None = None
    parser_version: str | None = None
    office_count: int = 0
    quota_count: int = 0
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.source_updated_at is not None and (
            self.source_updated_at.tzinfo is None
            or self.source_updated_at.utcoffset() is None
        ):
            raise ValueError("source_updated_at must be timezone-aware")
        if self.office_count < 0 or self.quota_count < 0:
            raise ValueError("observation counts must be >= 0")

    @property
    def may_update_state(self) -> bool:
        return self.outcome is ObservationOutcome.SUCCESS
