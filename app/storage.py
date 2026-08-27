"""SQLite storage bootstrap and persistence helpers for the small single-instance MVP.

The schema encodes the important reliability and product rules in the database where
possible: notification deduplication is UNIQUE, outbox work uses expiring leases,
source observations are stored separately from confirmed quota state, and subscription
records include the timestamps needed to evaluate the V1 task-oriented plans.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from .events import QuotaEvent, QuotaState
from .observations import QuotaObservation
from .quota import QuotaKey, QuotaStatus


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'fetch_error', 'parse_error', 'rejected')),
    source_updated_at TEXT,
    payload_hash TEXT,
    parser_version TEXT,
    office_count INTEGER NOT NULL DEFAULT 0 CHECK (office_count >= 0),
    quota_count INTEGER NOT NULL DEFAULT 0 CHECK (quota_count >= 0),
    error_code TEXT
);

CREATE INDEX IF NOT EXISTS idx_quota_observations_observed_at
ON quota_observations(observed_at);

CREATE TABLE IF NOT EXISTS quota_state (
    quota_date TEXT NOT NULL,
    office_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('unavailable', 'limited', 'available')),
    service_periods_json TEXT NOT NULL DEFAULT '[]',
    occurrence_id TEXT,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    source_updated_at TEXT,
    missing_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_count >= 0),
    PRIMARY KEY (quota_date, office_id)
);

CREATE TABLE IF NOT EXISTS quota_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quota_date TEXT NOT NULL,
    office_id TEXT NOT NULL,
    from_status TEXT NOT NULL CHECK (from_status IN ('unavailable', 'limited', 'available')),
    to_status TEXT NOT NULL CHECK (to_status IN ('limited', 'available')),
    occurrence_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source_updated_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (quota_date, office_id, occurrence_id, to_status)
);

CREATE INDEX IF NOT EXISTS idx_quota_events_observed_at
ON quota_events(observed_at);

CREATE TABLE IF NOT EXISTS matched_quota_events (
    quota_event_id INTEGER PRIMARY KEY REFERENCES quota_events(id) ON DELETE CASCADE,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_normalized TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    unsubscribed_at TEXT,
    consent_source TEXT NOT NULL,
    trial_used_at TEXT
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    plan_code TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    activated_at TEXT,
    original_expires_at TEXT,
    expires_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    guarantee_extended_at TEXT,
    first_matched_event_at TEXT,
    first_notification_queued_at TEXT,
    first_provider_accepted_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_customer
ON subscriptions(customer_id);

CREATE TABLE IF NOT EXISTS subscription_filters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    target_key TEXT NOT NULL DEFAULT 'default',
    earliest_date TEXT NOT NULL,
    deadline TEXT NOT NULL,
    office_id TEXT NOT NULL,
    minimum_status TEXT NOT NULL CHECK (minimum_status IN ('limited', 'available'))
);

CREATE INDEX IF NOT EXISTS idx_subscription_filters_subscription
ON subscription_filters(subscription_id);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER REFERENCES subscriptions(id),
    quota_event_id INTEGER REFERENCES quota_events(id),
    notification_kind TEXT NOT NULL DEFAULT 'quota_alert' CHECK (notification_kind IN ('verify_email', 'activation_test', 'quota_alert', 'manage_link')),
    dedup_key TEXT UNIQUE,
    channel TEXT NOT NULL,
    recipient_email TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    locked_at TEXT,
    locked_by TEXT,
    lock_expires_at TEXT,
    provider_message_id TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_notification_outbox_work
ON notification_outbox(status, next_attempt_at, lock_expires_at);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id INTEGER NOT NULL REFERENCES notification_outbox(id) ON DELETE CASCADE,
    attempted_at TEXT NOT NULL,
    provider_message_id TEXT,
    result TEXT NOT NULL,
    error_code TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    plan_code TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL,
    external_reference TEXT,
    status TEXT NOT NULL,
    paid_at TEXT
);

CREATE TABLE IF NOT EXISTS activation_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,
    plan_code TEXT NOT NULL CHECK (plan_code IN ('trial', 'quick', 'goal', 'family')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('available', 'reserved', 'redeemed', 'revoked', 'expired')),
    redeemed_at TEXT,
    redeemed_customer_id INTEGER REFERENCES customers(id),
    order_reference TEXT,
    reserved_at TEXT,
    reservation_expires_at TEXT,
    reserved_customer_id INTEGER REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS email_verification_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    activation_code_id INTEGER NOT NULL REFERENCES activation_codes(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    targets_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS magic_link_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    subscription_id INTEGER REFERENCES subscriptions(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS subscription_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    email_normalized TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subscription_id, email_normalized)
);
"""


def connect_database(path: Path | str) -> sqlite3.Connection:
    """Open SQLite with the MVP safety settings enabled."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Add V1 product-plan fields to databases created by schema version 1."""

    migrations = [
        ("customers", "trial_used_at", "ALTER TABLE customers ADD COLUMN trial_used_at TEXT"),
        ("subscriptions", "activated_at", "ALTER TABLE subscriptions ADD COLUMN activated_at TEXT"),
        (
            "subscriptions",
            "original_expires_at",
            "ALTER TABLE subscriptions ADD COLUMN original_expires_at TEXT",
        ),
        (
            "subscriptions",
            "guarantee_extended_at",
            "ALTER TABLE subscriptions ADD COLUMN guarantee_extended_at TEXT",
        ),
        (
            "subscriptions",
            "first_matched_event_at",
            "ALTER TABLE subscriptions ADD COLUMN first_matched_event_at TEXT",
        ),
        (
            "subscriptions",
            "first_notification_queued_at",
            "ALTER TABLE subscriptions ADD COLUMN first_notification_queued_at TEXT",
        ),
        (
            "subscriptions",
            "first_provider_accepted_at",
            "ALTER TABLE subscriptions ADD COLUMN first_provider_accepted_at TEXT",
        ),
        (
            "subscription_filters",
            "target_key",
            "ALTER TABLE subscription_filters ADD COLUMN target_key TEXT NOT NULL DEFAULT 'default'",
        ),
    ]

    for table, column, statement in migrations:
        if not _column_exists(connection, table, column):
            connection.execute(statement)


def _ensure_v2_indexes(connection: sqlite3.Connection) -> None:
    """Create indexes that depend on columns introduced by schema v2."""

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_subscription_filters_target
        ON subscription_filters(subscription_id, target_key)
        """
    )


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """Generalize the quota-only outbox without losing queued legacy rows."""

    if _column_exists(connection, "notification_outbox", "notification_kind"):
        return

    connection.execute("ALTER TABLE delivery_attempts RENAME TO delivery_attempts_v2")
    connection.execute("ALTER TABLE notification_outbox RENAME TO notification_outbox_v2")
    connection.execute("DROP INDEX IF EXISTS idx_notification_outbox_work")
    connection.execute(
        """
        CREATE TABLE notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER REFERENCES subscriptions(id),
            quota_event_id INTEGER REFERENCES quota_events(id),
            notification_kind TEXT NOT NULL DEFAULT 'quota_alert' CHECK (notification_kind IN
                ('verify_email', 'activation_test', 'quota_alert', 'manage_link')),
            dedup_key TEXT UNIQUE,
            channel TEXT NOT NULL,
            recipient_email TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL CHECK (status IN
                ('pending', 'sending', 'sent', 'failed', 'cancelled')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at TEXT,
            locked_at TEXT,
            locked_by TEXT,
            lock_expires_at TEXT,
            provider_message_id TEXT,
            last_error_code TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE delivery_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outbox_id INTEGER NOT NULL REFERENCES notification_outbox(id) ON DELETE CASCADE,
            attempted_at TEXT NOT NULL,
            provider_message_id TEXT,
            result TEXT NOT NULL,
            error_code TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO notification_outbox(
            id, subscription_id, quota_event_id, notification_kind, dedup_key,
            channel, recipient_email, payload_json, status, attempt_count,
            next_attempt_at, locked_at, locked_by, lock_expires_at,
            provider_message_id, created_at, sent_at
        )
        SELECT o.id, o.subscription_id, o.quota_event_id, 'quota_alert',
               'legacy:quota_alert:' || o.subscription_id || ':' || o.quota_event_id || ':' || o.channel,
               o.channel, c.email_normalized, '{}', o.status, o.attempt_count,
               o.next_attempt_at, o.locked_at, o.locked_by, o.lock_expires_at,
               o.provider_message_id, o.created_at, o.sent_at
        FROM notification_outbox_v2 AS o
        JOIN subscriptions AS s ON s.id = o.subscription_id
        JOIN customers AS c ON c.id = s.customer_id
        """
    )
    connection.execute(
        """
        INSERT INTO delivery_attempts(id, outbox_id, attempted_at, provider_message_id, result, error_code)
        SELECT id, outbox_id, attempted_at, provider_message_id, result, error_code
        FROM delivery_attempts_v2
        """
    )
    connection.execute("DROP TABLE delivery_attempts_v2")
    connection.execute("DROP TABLE notification_outbox_v2")


def _ensure_v3_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_notification_outbox_work
        ON notification_outbox(status, next_attempt_at, lock_expires_at)
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_activation_codes_status ON activation_codes(status, expires_at)"
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_outbox_legacy_dedup
        AFTER INSERT ON notification_outbox
        WHEN NEW.dedup_key IS NULL
        BEGIN
            UPDATE notification_outbox
            SET dedup_key = 'legacy:' || COALESCE(NEW.subscription_id, 'none') || ':' ||
                            COALESCE(NEW.quota_event_id, 'none') || ':' || NEW.channel
            WHERE id = NEW.id;
        END
        """
    )


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create or upgrade the SQLite schema idempotently."""

    current_version = connection.execute("PRAGMA user_version").fetchone()[0]

    # Create all tables and indexes that are safe for both fresh and legacy DBs.
    # Version-dependent indexes are created only after missing columns are added.
    connection.executescript(SCHEMA_SQL)

    if current_version < 2:
        _migrate_v1_to_v2(connection)

    _ensure_v2_indexes(connection)
    if current_version < 3:
        _migrate_v2_to_v3(connection)
    _ensure_v3_indexes(connection)
    connection.execute("PRAGMA user_version = 3")
    connection.commit()


def _datetime_to_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("database datetimes must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _datetime_from_text(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored datetime must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def set_runtime_state(
    connection: sqlite3.Connection,
    key: str,
    value: str,
    *,
    updated_at: datetime,
) -> None:
    if not key.strip():
        raise ValueError("runtime state key must not be empty")
    connection.execute(
        """
        INSERT INTO runtime_state(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value, _datetime_to_text(updated_at)),
    )


def get_runtime_state(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute(
        "SELECT value FROM runtime_state WHERE key = ?",
        (key,),
    ).fetchone()
    return None if row is None else str(row["value"])


def record_observation(
    connection: sqlite3.Connection,
    observation: QuotaObservation,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO quota_observations(
            observed_at, outcome, source_updated_at, payload_hash, parser_version,
            office_count, quota_count, error_code
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _datetime_to_text(observation.observed_at),
            observation.outcome.value,
            _datetime_to_text(observation.source_updated_at),
            observation.payload_hash,
            observation.parser_version,
            observation.office_count,
            observation.quota_count,
            observation.error_code,
        ),
    )
    return int(cursor.lastrowid)


def load_quota_states(connection: sqlite3.Connection) -> dict[QuotaKey, QuotaState]:
    rows = connection.execute(
        """
        SELECT quota_date, office_id, status, service_periods_json, occurrence_id,
               first_observed_at, last_observed_at, source_updated_at, missing_count
        FROM quota_state
        """
    ).fetchall()

    states: dict[QuotaKey, QuotaState] = {}
    for row in rows:
        raw_periods = json.loads(row["service_periods_json"])
        if not isinstance(raw_periods, list) or not all(
            isinstance(period, str) for period in raw_periods
        ):
            raise ValueError("invalid service_periods_json in quota_state")

        key = QuotaKey(date.fromisoformat(row["quota_date"]), str(row["office_id"]))
        first_observed_at = _datetime_from_text(row["first_observed_at"])
        last_observed_at = _datetime_from_text(row["last_observed_at"])
        if first_observed_at is None or last_observed_at is None:
            raise ValueError("quota_state observed timestamps must not be null")

        states[key] = QuotaState(
            key=key,
            status=QuotaStatus(str(row["status"])),
            occurrence_id=row["occurrence_id"],
            first_observed_at=first_observed_at,
            last_observed_at=last_observed_at,
            source_updated_at=_datetime_from_text(row["source_updated_at"]),
            service_periods=tuple(raw_periods),
            missing_count=int(row["missing_count"]),
        )
    return states


def save_quota_states(
    connection: sqlite3.Connection,
    states: Iterable[QuotaState],
) -> None:
    connection.executemany(
        """
        INSERT INTO quota_state(
            quota_date, office_id, status, service_periods_json, occurrence_id,
            first_observed_at, last_observed_at, source_updated_at, missing_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(quota_date, office_id) DO UPDATE SET
            status = excluded.status,
            service_periods_json = excluded.service_periods_json,
            occurrence_id = excluded.occurrence_id,
            first_observed_at = excluded.first_observed_at,
            last_observed_at = excluded.last_observed_at,
            source_updated_at = excluded.source_updated_at,
            missing_count = excluded.missing_count
        """,
        [
            (
                state.key.date.isoformat(),
                state.key.office_id,
                state.status.value,
                json.dumps(list(state.service_periods), separators=(",", ":")),
                state.occurrence_id,
                _datetime_to_text(state.first_observed_at),
                _datetime_to_text(state.last_observed_at),
                _datetime_to_text(state.source_updated_at),
                state.missing_count,
            )
            for state in states
        ],
    )


def insert_quota_events(
    connection: sqlite3.Connection,
    events: Iterable[QuotaEvent],
    *,
    created_at: datetime,
) -> list[int]:
    inserted_ids: list[int] = []
    created_at_text = _datetime_to_text(created_at)
    for event in events:
        cursor = connection.execute(
            """
            INSERT INTO quota_events(
                quota_date, office_id, from_status, to_status, occurrence_id,
                observed_at, source_updated_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(quota_date, office_id, occurrence_id, to_status) DO NOTHING
            """,
            (
                event.key.date.isoformat(),
                event.key.office_id,
                event.from_status.value,
                event.to_status.value,
                event.occurrence_id,
                _datetime_to_text(event.observed_at),
                _datetime_to_text(event.source_updated_at),
                created_at_text,
            ),
        )
        if cursor.rowcount == 1:
            inserted_ids.append(int(cursor.lastrowid))
    return inserted_ids
