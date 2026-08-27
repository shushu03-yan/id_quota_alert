"""Small operator-facing read helpers for the early paid-pilot workflow."""

from __future__ import annotations

import sqlite3

from .storage import initialize_database


def list_customers(connection: sqlite3.Connection) -> list[str]:
    initialize_database(connection)
    rows = connection.execute(
        """SELECT c.id, c.email_normalized, c.created_at, c.unsubscribed_at,
                  COUNT(s.id) AS subscriptions
           FROM customers c LEFT JOIN subscriptions s ON s.customer_id=c.id
           GROUP BY c.id ORDER BY c.id DESC"""
    ).fetchall()
    return [
        "id={id} email={email} subscriptions={subscriptions} created_at={created} status={status}".format(
            id=row["id"],
            email=_mask_email(str(row["email_normalized"])),
            subscriptions=row["subscriptions"],
            created=row["created_at"],
            status="unsubscribed" if row["unsubscribed_at"] else "active",
        )
        for row in rows
    ]


def list_subscriptions(connection: sqlite3.Connection) -> list[str]:
    initialize_database(connection)
    rows = connection.execute(
        """SELECT s.id, s.plan_code, s.active, s.activated_at, s.expires_at,
                  c.email_normalized,
                  COUNT(DISTINCT f.target_key) AS targets
           FROM subscriptions s
           JOIN customers c ON c.id=s.customer_id
           LEFT JOIN subscription_filters f ON f.subscription_id=s.id
           GROUP BY s.id ORDER BY s.id DESC"""
    ).fetchall()
    return [
        "id={id} plan={plan} email={email} active={active} targets={targets} activated_at={activated} expires_at={expires}".format(
            id=row["id"], plan=row["plan_code"], email=_mask_email(str(row["email_normalized"])),
            active=bool(row["active"]), targets=row["targets"], activated=row["activated_at"], expires=row["expires_at"],
        )
        for row in rows
    ]


def show_subscription(connection: sqlite3.Connection, subscription_id: int) -> list[str]:
    initialize_database(connection)
    row = connection.execute(
        """SELECT s.*, c.email_normalized, c.unsubscribed_at
           FROM subscriptions s JOIN customers c ON c.id=s.customer_id WHERE s.id=?""",
        (subscription_id,),
    ).fetchone()
    if row is None:
        raise ValueError("subscription not found")
    lines = [
        f"id={row['id']} plan={row['plan_code']} active={bool(row['active'])}",
        f"email={_mask_email(str(row['email_normalized']))}",
        f"activated_at={row['activated_at']} expires_at={row['expires_at']} original_expires_at={row['original_expires_at']}",
        f"first_matched_event_at={row['first_matched_event_at']} first_notification_queued_at={row['first_notification_queued_at']} first_provider_accepted_at={row['first_provider_accepted_at']}",
        f"guarantee_extended_at={row['guarantee_extended_at']} unsubscribed_at={row['unsubscribed_at']}",
    ]
    targets = connection.execute(
        """SELECT target_key, earliest_date, deadline, office_id, minimum_status
           FROM subscription_filters WHERE subscription_id=? ORDER BY target_key,id""",
        (subscription_id,),
    ).fetchall()
    lines.extend(
        f"target={item['target_key']} dates={item['earliest_date']}..{item['deadline']} office={item['office_id']} minimum={item['minimum_status']}"
        for item in targets
    )
    return lines


def outbox_status(connection: sqlite3.Connection) -> list[str]:
    initialize_database(connection)
    rows = connection.execute(
        """SELECT notification_kind, status, COUNT(*) AS count
           FROM notification_outbox GROUP BY notification_kind,status
           ORDER BY notification_kind,status"""
    ).fetchall()
    if not rows:
        return ["outbox empty"]
    return [f"kind={row['notification_kind']} status={row['status']} count={row['count']}" for row in rows]


def _mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    if not domain:
        return "***"
    visible = local[:1] if local else ""
    return f"{visible}***@{domain}"
