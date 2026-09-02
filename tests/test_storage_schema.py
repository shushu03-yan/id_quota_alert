import sqlite3

import pytest

from app.storage import connect_database, initialize_database


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def test_schema_contains_observation_state_event_and_outbox_tables(tmp_path) -> None:
    connection = connect_database(tmp_path / "quota.sqlite3")
    initialize_database(connection)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }

    assert {
        "quota_observations",
        "quota_state",
        "quota_events",
        "customers",
        "subscriptions",
        "subscription_filters",
        "notification_outbox",
        "delivery_attempts",
        "orders",
    }.issubset(tables)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_v1_product_fields_are_present(tmp_path) -> None:
    connection = connect_database(tmp_path / "quota.sqlite3")
    initialize_database(connection)

    assert "trial_used_at" in _columns(connection, "customers")

    subscription_columns = _columns(connection, "subscriptions")
    assert {
        "activated_at",
        "original_expires_at",
        "guarantee_extended_at",
        "first_matched_event_at",
        "first_notification_queued_at",
        "first_provider_accepted_at",
    }.issubset(subscription_columns)

    assert "target_key" in _columns(connection, "subscription_filters")


def test_subscription_target_can_include_multiple_offices(tmp_path) -> None:
    connection = connect_database(tmp_path / "quota.sqlite3")
    initialize_database(connection)

    customer_id = connection.execute(
        """
        INSERT INTO customers(email_normalized, created_at, consent_source)
        VALUES ('target@example.com', '2026-08-27T15:00:00Z', 'test')
        """
    ).lastrowid
    subscription_id = connection.execute(
        """
        INSERT INTO subscriptions(
            customer_id, plan_code, starts_at, activated_at, original_expires_at,
            expires_at, active, created_at
        ) VALUES (?, 'quick', '2026-08-27T15:00:00Z', '2026-08-27T15:00:00Z',
                  '2026-08-30T15:00:00Z', '2026-08-30T15:00:00Z', 1,
                  '2026-08-27T15:00:00Z')
        """,
        (customer_id,),
    ).lastrowid

    connection.executemany(
        """
        INSERT INTO subscription_filters(
            subscription_id, target_key, earliest_date, deadline, office_id,
            minimum_status
        ) VALUES (?, 'target-a', '2026-09-01', '2026-09-10', ?, 'limited')
        """,
        [(subscription_id, "sha-tin"), (subscription_id, "fo-tan")],
    )

    offices = {
        row[0]
        for row in connection.execute(
            """
            SELECT office_id
            FROM subscription_filters
            WHERE subscription_id = ? AND target_key = 'target-a'
            """,
            (subscription_id,),
        ).fetchall()
    }
    assert offices == {"sha-tin", "fo-tan"}


def test_outbox_deduplication_is_enforced_by_database(tmp_path) -> None:
    connection = connect_database(tmp_path / "quota.sqlite3")
    initialize_database(connection)

    customer_id = connection.execute(
        """
        INSERT INTO customers(email_normalized, created_at, consent_source)
        VALUES ('user@example.com', '2026-08-27T15:00:00Z', 'test')
        """
    ).lastrowid
    subscription_id = connection.execute(
        """
        INSERT INTO subscriptions(
            customer_id, plan_code, starts_at, expires_at, active, created_at
        ) VALUES (?, 'test', '2026-08-27T15:00:00Z', '2026-09-10T15:00:00Z', 1,
                  '2026-08-27T15:00:00Z')
        """,
        (customer_id,),
    ).lastrowid
    event_id = connection.execute(
        """
        INSERT INTO quota_events(
            quota_date, office_id, from_status, to_status, occurrence_id,
            observed_at, created_at
        ) VALUES ('2026-09-01', 'office-a', 'unavailable', 'available', 'occ-1',
                  '2026-08-27T15:01:00Z', '2026-08-27T15:01:00Z')
        """
    ).lastrowid

    connection.execute(
        """
        INSERT INTO notification_outbox(
            subscription_id, quota_event_id, channel, status, created_at
        ) VALUES (?, ?, 'email', 'pending', '2026-08-27T15:01:00Z')
        """,
        (subscription_id, event_id),
    )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO notification_outbox(
                subscription_id, quota_event_id, channel, status, created_at
            ) VALUES (?, ?, 'email', 'pending', '2026-08-27T15:01:01Z')
            """,
            (subscription_id, event_id),
        )


def test_v1_database_can_be_upgraded_to_current_schema(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = connect_database(path)

    connection.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_normalized TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            unsubscribed_at TEXT,
            consent_source TEXT NOT NULL
        );
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            plan_code TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE subscription_filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
            earliest_date TEXT NOT NULL,
            deadline TEXT NOT NULL,
            office_id TEXT NOT NULL,
            minimum_status TEXT NOT NULL
        );
        PRAGMA user_version = 1;
        """
    )
    connection.commit()

    initialize_database(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert "trial_used_at" in _columns(connection, "customers")
    assert "guarantee_extended_at" in _columns(connection, "subscriptions")
    assert "target_key" in _columns(connection, "subscription_filters")


def test_v2_outbox_is_migrated_without_losing_rows_or_attempts(tmp_path) -> None:
    connection = connect_database(tmp_path / "legacy-v2.sqlite3")
    connection.executescript(
        """
        CREATE TABLE customers(id INTEGER PRIMARY KEY, email_normalized TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL, unsubscribed_at TEXT, consent_source TEXT NOT NULL, trial_used_at TEXT);
        CREATE TABLE subscriptions(id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
            plan_code TEXT NOT NULL, starts_at TEXT NOT NULL, activated_at TEXT, original_expires_at TEXT,
            expires_at TEXT NOT NULL, active INTEGER NOT NULL, guarantee_extended_at TEXT,
            first_matched_event_at TEXT, first_notification_queued_at TEXT, first_provider_accepted_at TEXT,
            created_at TEXT NOT NULL);
        CREATE TABLE quota_events(id INTEGER PRIMARY KEY, quota_date TEXT NOT NULL, office_id TEXT NOT NULL,
            from_status TEXT NOT NULL, to_status TEXT NOT NULL, occurrence_id TEXT NOT NULL,
            observed_at TEXT NOT NULL, source_updated_at TEXT, created_at TEXT NOT NULL,
            UNIQUE(quota_date,office_id,occurrence_id,to_status));
        CREATE TABLE notification_outbox(id INTEGER PRIMARY KEY, subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
            quota_event_id INTEGER NOT NULL REFERENCES quota_events(id), channel TEXT NOT NULL, status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, locked_at TEXT, locked_by TEXT,
            lock_expires_at TEXT, provider_message_id TEXT, created_at TEXT NOT NULL, sent_at TEXT,
            UNIQUE(subscription_id,quota_event_id,channel));
        CREATE TABLE delivery_attempts(id INTEGER PRIMARY KEY, outbox_id INTEGER NOT NULL REFERENCES notification_outbox(id),
            attempted_at TEXT NOT NULL, provider_message_id TEXT, result TEXT NOT NULL, error_code TEXT);
        INSERT INTO customers VALUES (1,'legacy@example.com','2026-08-27T00:00:00Z',NULL,'test',NULL);
        INSERT INTO subscriptions VALUES (1,1,'goal','2026-08-27T00:00:00Z','2026-08-27T00:00:00Z',
            '2026-09-10T00:00:00Z','2026-09-10T00:00:00Z',1,NULL,NULL,NULL,NULL,'2026-08-27T00:00:00Z');
        INSERT INTO quota_events VALUES (1,'2026-09-01','office-a','unavailable','available','occ-1',
            '2026-08-27T01:00:00Z',NULL,'2026-08-27T01:00:00Z');
        INSERT INTO notification_outbox VALUES (1,1,1,'email','pending',1,NULL,NULL,NULL,NULL,NULL,'2026-08-27T01:00:00Z',NULL);
        INSERT INTO delivery_attempts VALUES (1,1,'2026-08-27T01:01:00Z',NULL,'retryable_failure','timeout');
        PRAGMA user_version=2;
        """
    )
    connection.commit()
    initialize_database(connection)
    row = connection.execute("SELECT notification_kind,dedup_key,recipient_email,attempt_count FROM notification_outbox").fetchone()
    assert tuple(row) == ("quota_alert", "legacy:quota_alert:1:1:email", "legacy@example.com", 1)
    assert connection.execute("SELECT result,error_code FROM delivery_attempts").fetchone()[:] == ("retryable_failure", "timeout")
    initialize_database(connection)
    assert connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1


def test_v3_timestamp_regressions_are_migrated_to_stale(tmp_path) -> None:
    connection = connect_database(tmp_path / "legacy-v3.sqlite3")
    connection.executescript(
        """
        CREATE TABLE quota_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN
                ('success', 'fetch_error', 'parse_error', 'rejected')),
            source_updated_at TEXT,
            payload_hash TEXT,
            parser_version TEXT,
            office_count INTEGER NOT NULL DEFAULT 0,
            quota_count INTEGER NOT NULL DEFAULT 0,
            error_code TEXT
        );
        CREATE INDEX idx_quota_observations_observed_at
        ON quota_observations(observed_at);
        INSERT INTO quota_observations(
            observed_at, outcome, payload_hash, parser_version, error_code
        ) VALUES (
            '2026-09-02T00:00:00Z', 'rejected', 'old-hash',
            'govhk-get-situation-v1', 'source_time_regression'
        );
        PRAGMA user_version = 3;
        """
    )
    connection.commit()

    initialize_database(connection)

    row = connection.execute(
        "SELECT id, outcome, payload_hash, error_code FROM quota_observations"
    ).fetchone()
    assert tuple(row) == (1, "stale", "old-hash", "source_time_regression")
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    connection.execute(
        """
        INSERT INTO quota_observations(observed_at, outcome, error_code)
        VALUES ('2026-09-02T00:01:00Z', 'stale', 'source_time_regression')
        """
    )
