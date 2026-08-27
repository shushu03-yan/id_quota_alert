import sqlite3

import pytest

from app.storage import connect_database, initialize_database


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
        "notification_outbox",
        "delivery_attempts",
    }.issubset(tables)


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
