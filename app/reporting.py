"""Operator-facing health and soak summaries derived from persisted audit data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3

from .storage import _datetime_from_text, get_runtime_state, initialize_database


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _state_datetime(connection: sqlite3.Connection, key: str) -> datetime | None:
    return _datetime_from_text(get_runtime_state(connection, key))


def _display(value: str | None) -> str:
    return value or "never"


@dataclass(frozen=True, slots=True)
class HealthReport:
    values: dict[str, str | int]
    poller_status: str
    source_response_status: str
    source_status: str
    email_status: str

    def render(self) -> str:
        ordered = (
            "last_poll_attempt", "last_poll_outcome", "last_well_formed_poll",
            "last_successful_poll", "last_valid_snapshot", "last_source_updated_at",
            "last_received_source_updated_at", "last_stale_snapshot",
            "last_source_regression_seconds", "baseline_initialized",
            "observation_count", "success_count", "stale_count", "fetch_error_count",
            "parse_error_count", "rejected_count", "source_version_conflict_count",
            "latest_error_code",
            "quota_state_count", "quota_event_count", "last_successful_email",
        )
        lines = [f"{key}: {self.values[key]}" for key in ordered]
        lines += [
            "",
            f"Poller: {self.poller_status}",
            f"Source response: {self.source_response_status}",
            f"Source freshness: {self.source_status}",
            f"Email: {self.email_status}",
        ]
        return "\n".join(lines)


def build_health_report(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(minutes=5),
    email_stale_after: timedelta = timedelta(hours=24),
) -> HealthReport:
    initialize_database(connection)
    now = now or _now_utc()
    counts = {row["outcome"]: int(row["count"]) for row in connection.execute(
        "SELECT outcome, COUNT(*) AS count FROM quota_observations GROUP BY outcome"
    )}
    last_attempt = _state_datetime(connection, "last_poll_attempt")
    last_well_formed = _state_datetime(connection, "last_well_formed_poll")
    last_valid = _state_datetime(connection, "last_valid_snapshot")
    last_email = _state_datetime(connection, "last_successful_email")
    poller_status = "NOT RUN" if last_attempt is None else ("STALE" if now - last_attempt > stale_after else "HEALTHY")
    source_response_status = "NOT RUN" if last_well_formed is None else ("STALE" if now - last_well_formed > stale_after else "HEALTHY")
    source_status = "NOT RUN" if last_valid is None else ("STALE" if now - last_valid > stale_after else "HEALTHY")
    email_status = "NOT RUN" if last_email is None else ("STALE" if now - last_email > email_stale_after else "HEALTHY")
    latest_error = connection.execute(
        "SELECT error_code FROM quota_observations WHERE error_code IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    state = lambda key: _display(get_runtime_state(connection, key))
    values: dict[str, str | int] = {
        "last_poll_attempt": state("last_poll_attempt"),
        "last_poll_outcome": state("last_poll_outcome"),
        "last_well_formed_poll": state("last_well_formed_poll"),
        "last_successful_poll": state("last_successful_poll"),
        "last_valid_snapshot": state("last_valid_snapshot"),
        "last_source_updated_at": state("last_source_updated_at"),
        "last_received_source_updated_at": state("last_received_source_updated_at"),
        "last_stale_snapshot": state("last_stale_snapshot"),
        "last_source_regression_seconds": state("last_source_regression_seconds"),
        "baseline_initialized": get_runtime_state(connection, "baseline_initialized") or "0",
        "observation_count": sum(counts.values()),
        "success_count": counts.get("success", 0),
        "stale_count": counts.get("stale", 0),
        "fetch_error_count": counts.get("fetch_error", 0),
        "parse_error_count": counts.get("parse_error", 0),
        "rejected_count": counts.get("rejected", 0),
        "source_version_conflict_count": int(connection.execute(
            "SELECT COUNT(*) FROM quota_observations WHERE error_code = 'source_version_conflict'"
        ).fetchone()[0]),
        "latest_error_code": "none" if latest_error is None else str(latest_error[0]),
        "quota_state_count": int(connection.execute("SELECT COUNT(*) FROM quota_state").fetchone()[0]),
        "quota_event_count": int(connection.execute("SELECT COUNT(*) FROM quota_events").fetchone()[0]),
        "last_successful_email": state("last_successful_email"),
    }
    return HealthReport(values, poller_status, source_response_status, source_status, email_status)


@dataclass(frozen=True, slots=True)
class SoakSummary:
    values: dict[str, str | int]
    complete: bool

    def render(self) -> str:
        lines = [f"{key}: {value}" for key, value in self.values.items()]
        lines += ["", "SOAK WINDOW COMPLETE — MANUAL REVIEW REQUIRED" if self.complete else "SOAK TEST NOT COMPLETE"]
        return "\n".join(lines)


def build_soak_summary(connection: sqlite3.Connection) -> SoakSummary:
    initialize_database(connection)
    rows = connection.execute(
        "SELECT outcome, COUNT(*) AS count FROM quota_observations GROUP BY outcome"
    ).fetchall()
    counts = {str(row["outcome"]): int(row["count"]) for row in rows}
    total = sum(counts.values())
    bounds = connection.execute("SELECT MIN(observed_at), MAX(observed_at) FROM quota_observations").fetchone()
    first = _datetime_from_text(bounds[0])
    last = _datetime_from_text(bounds[1])
    duration = timedelta(0) if first is None or last is None else last - first
    errors = {str(row["error_code"]): int(row["count"]) for row in connection.execute(
        "SELECT error_code, COUNT(*) AS count FROM quota_observations WHERE error_code IS NOT NULL GROUP BY error_code"
    )}
    pct = lambda kind: f"{(counts.get(kind, 0) * 100 / total):.2f}%" if total else "0.00%"
    values: dict[str, str | int] = {
        "total_observations": total,
        "success_percent": pct("success"),
        "stale_percent": pct("stale"),
        "fetch_error_percent": pct("fetch_error"),
        "parse_error_percent": pct("parse_error"),
        "rejected_percent": pct("rejected"),
        "timeout_count": sum(v for k, v in errors.items() if "timeout" in k.lower()),
        "403_count": sum(v for k, v in errors.items() if "403" in k),
        "429_count": sum(v for k, v in errors.items() if "429" in k),
        "5xx_count": sum(
            v
            for k, v in errors.items()
            if k == "http_5xx" or any(str(code) in k for code in range(500, 600))
        ),
        "source_time_regression_count": errors.get("source_time_regression", 0),
        "source_version_conflict_count": errors.get("source_version_conflict", 0),
        "quota_events_count": int(connection.execute("SELECT COUNT(*) FROM quota_events").fetchone()[0]),
        "current_state_count": int(connection.execute("SELECT COUNT(*) FROM quota_state").fetchone()[0]),
        "first_observation": "never" if first is None else first.isoformat().replace("+00:00", "Z"),
        "last_observation": "never" if last is None else last.isoformat().replace("+00:00", "Z"),
        "runtime_duration": str(duration),
    }
    return SoakSummary(values, total > 0 and duration >= timedelta(days=3))
