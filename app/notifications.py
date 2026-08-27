"""Notification outbox, quota matcher, leasing worker, retry and delivery audit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import os
import sqlite3

from .config import validate_token_signing_secret
from .email_provider import EmailDeliveryResult, EmailProvider
from .storage import _datetime_to_text, initialize_database, set_runtime_state


STATUS_RANK = {"unavailable": 0, "limited": 1, "available": 2}


def queue_notification(
    connection: sqlite3.Connection,
    *,
    notification_kind: str,
    dedup_key: str,
    recipient_email: str,
    payload: dict[str, object],
    created_at: datetime,
    subscription_id: int | None = None,
    quota_event_id: int | None = None,
) -> tuple[int, bool]:
    cursor = connection.execute(
        """
        INSERT INTO notification_outbox(
            subscription_id, quota_event_id, notification_kind, dedup_key, channel,
            recipient_email, payload_json, status, next_attempt_at, created_at
        ) VALUES (?, ?, ?, ?, 'email', ?, ?, 'pending', ?, ?)
        ON CONFLICT(dedup_key) DO NOTHING
        """,
        (subscription_id, quota_event_id, notification_kind, dedup_key,
         recipient_email, json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
         _datetime_to_text(created_at), _datetime_to_text(created_at)),
    )
    if cursor.rowcount == 1:
        return int(cursor.lastrowid), True
    row = connection.execute("SELECT id FROM notification_outbox WHERE dedup_key = ?", (dedup_key,)).fetchone()
    if row is None:
        raise RuntimeError("deduplicated outbox row disappeared")
    return int(row[0]), False


def match_quota_event(connection: sqlite3.Connection, event_id: int, *, now: datetime) -> int:
    """Queue one alert per matching active subscription and return newly queued count."""
    initialize_database(connection)
    event = connection.execute("SELECT * FROM quota_events WHERE id = ?", (event_id,)).fetchone()
    if event is None:
        raise ValueError("quota event does not exist")
    rows = connection.execute(
        """
        SELECT DISTINCT s.id AS subscription_id, c.email_normalized
        FROM subscriptions AS s
        JOIN customers AS c ON c.id = s.customer_id
        JOIN subscription_filters AS f ON f.subscription_id = s.id
        WHERE s.active = 1 AND s.activated_at IS NOT NULL
          AND s.starts_at <= ? AND s.expires_at > ?
          AND ? >= s.starts_at AND ? >= s.activated_at
          AND c.unsubscribed_at IS NULL
          AND f.earliest_date <= ? AND f.deadline >= ?
          AND (f.office_id = ? OR f.office_id = '*')
        """,
        (_datetime_to_text(now), _datetime_to_text(now), event["observed_at"], event["observed_at"], event["quota_date"],
         event["quota_date"], event["office_id"]),
    ).fetchall()
    queued = 0
    for row in rows:
        minimums = connection.execute(
            """SELECT minimum_status FROM subscription_filters
               WHERE subscription_id = ? AND earliest_date <= ? AND deadline >= ?
                 AND (office_id = ? OR office_id = '*')""",
            (row["subscription_id"], event["quota_date"], event["quota_date"], event["office_id"]),
        ).fetchall()
        if not any(STATUS_RANK[event["to_status"]] >= STATUS_RANK[x[0]] for x in minimums):
            continue
        now_text = _datetime_to_text(now)
        connection.execute(
            "UPDATE subscriptions SET first_matched_event_at = COALESCE(first_matched_event_at, ?) WHERE id = ?",
            (event["observed_at"], row["subscription_id"]),
        )
        recipients = [str(x[0]) for x in connection.execute(
            "SELECT email_normalized FROM subscription_recipients WHERE subscription_id=? ORDER BY id",
            (row["subscription_id"],),
        ).fetchall()] or [str(row["email_normalized"])]
        created_for_subscription = False
        for recipient in recipients:
            _, created = queue_notification(
                connection,
                notification_kind="quota_alert",
                dedup_key=f"quota_alert:{row['subscription_id']}:{event_id}:{recipient}",
                recipient_email=recipient,
                payload={"quota_date": event["quota_date"], "office_id": event["office_id"], "status": event["to_status"]},
                created_at=now,
                subscription_id=int(row["subscription_id"]),
                quota_event_id=event_id,
            )
            if created:
                queued += 1
                created_for_subscription = True
        if created_for_subscription:
            connection.execute(
                "UPDATE subscriptions SET first_notification_queued_at = COALESCE(first_notification_queued_at, ?) WHERE id = ?",
                (now_text, row["subscription_id"]),
            )
    connection.commit()
    return queued


def match_pending_events(connection: sqlite3.Connection, *, now: datetime) -> int:
    """Process persisted events exactly once; outbox dedup remains the final safety net."""
    initialize_database(connection)
    queued = 0
    event_ids = [int(row[0]) for row in connection.execute(
        """SELECT q.id FROM quota_events q LEFT JOIN matched_quota_events m
           ON m.quota_event_id=q.id WHERE m.quota_event_id IS NULL ORDER BY q.id"""
    ).fetchall()]
    for event_id in event_ids:
        queued += match_quota_event(connection, event_id, now=now)
        connection.execute(
            "INSERT OR IGNORE INTO matched_quota_events(quota_event_id, processed_at) VALUES (?, ?)",
            (event_id, _datetime_to_text(now)),
        )
        connection.commit()
    return queued


@dataclass(frozen=True, slots=True)
class ClaimedNotification:
    id: int
    kind: str
    recipient: str
    payload: dict[str, object]
    attempt_count: int
    subscription_id: int | None


def claim_notification(
    connection: sqlite3.Connection, *, worker_id: str, now: datetime,
    lease_duration: timedelta = timedelta(minutes=5), max_attempts: int = 5,
) -> ClaimedNotification | None:
    initialize_database(connection)
    now_text = _datetime_to_text(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """
            SELECT * FROM notification_outbox
            WHERE attempt_count < ? AND (
                (status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                OR (status = 'sending' AND lock_expires_at <= ?)
            ) ORDER BY created_at, id LIMIT 1
            """, (max_attempts, now_text, now_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        connection.execute(
            """UPDATE notification_outbox SET status='sending', locked_at=?, locked_by=?,
               lock_expires_at=?, attempt_count=attempt_count+1 WHERE id=?""",
            (now_text, worker_id, _datetime_to_text(now + lease_duration), row["id"]),
        )
        connection.commit()
        return ClaimedNotification(int(row["id"]), str(row["notification_kind"]),
            str(row["recipient_email"]), json.loads(row["payload_json"]),
            int(row["attempt_count"]) + 1,
            None if row["subscription_id"] is None else int(row["subscription_id"]))
    except BaseException:
        connection.rollback()
        raise


def _message_for(item: ClaimedNotification) -> tuple[str, str]:
    if item.kind == "activation_test":
        return "你的 HKID Alert 已成功启动", str(item.payload.get("message", "这是一封服务启动确认邮件，不是实际配额提醒，不代表现在有预约名额。"))
    if item.kind == "verify_email":
        secret = validate_token_signing_secret(
            os.getenv("TOKEN_SIGNING_SECRET", ""), app_env=os.getenv("APP_ENV", "development")
        )
        record_id = int(item.payload["verification_id"])
        identifier = base64.urlsafe_b64encode(str(record_id).encode()).decode().rstrip("=")
        signature = hmac.new(secret.encode(), f"verify:{identifier}".encode(), hashlib.sha256).digest()
        token = f"{identifier}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"
        url = f"{str(item.payload['base_url']).rstrip('/')}/verify?token={token}"
        return "验证你的 HKID Alert 邮箱", f"请打开验证链接：{url}"
    if item.kind == "manage_link":
        secret = validate_token_signing_secret(
            os.getenv("TOKEN_SIGNING_SECRET", ""), app_env=os.getenv("APP_ENV", "development")
        )
        record_id = int(item.payload["magic_link_id"])
        identifier = base64.urlsafe_b64encode(str(record_id).encode()).decode().rstrip("=")
        signature = hmac.new(secret.encode(), f"manage:{identifier}".encode(), hashlib.sha256).digest()
        token = f"{identifier}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"
        url = f"{str(item.payload['base_url']).rstrip('/')}/manage?token={token}"
        return "管理你的 HKID Alert", f"请打开管理链接：{url}"
    return "HKID 公开预约配额变化提醒", f"日期：{item.payload.get('quota_date')}\n办事处：{item.payload.get('office_id')}\n状态：{item.payload.get('status')}\n\n请自行进入官方系统预约。本服务并非香港政府官方服务。"


RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1))


class EmailWorker:
    def __init__(self, connection: sqlite3.Connection, provider: EmailProvider, *, worker_id: str) -> None:
        self.connection = connection
        self.provider = provider
        self.worker_id = worker_id

    def run_once(self, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        match_pending_events(self.connection, now=now)
        item = claim_notification(self.connection, worker_id=self.worker_id, now=now)
        if item is None:
            return False
        if item.kind == "quota_alert" and item.subscription_id is not None:
            unsubscribed = self.connection.execute(
                """SELECT c.unsubscribed_at FROM subscriptions s JOIN customers c ON c.id=s.customer_id
                   WHERE s.id=?""", (item.subscription_id,),
            ).fetchone()
            if unsubscribed is None or unsubscribed[0] is not None:
                self.connection.execute(
                    "UPDATE notification_outbox SET status='cancelled', locked_at=NULL, locked_by=NULL, lock_expires_at=NULL WHERE id=?",
                    (item.id,),
                )
                self.connection.commit()
                return True
        subject, text = _message_for(item)
        result = self.provider.send(recipient=item.recipient, subject=subject, text=text)
        self._finish(item, result, now)
        return True

    def _finish(self, item: ClaimedNotification, result: EmailDeliveryResult, now: datetime) -> None:
        self.connection.execute(
            "INSERT INTO delivery_attempts(outbox_id, attempted_at, provider_message_id, result, error_code) VALUES (?, ?, ?, ?, ?)",
            (item.id, _datetime_to_text(now), result.provider_message_id,
             "accepted" if result.accepted else ("retryable_failure" if result.retryable else "permanent_failure"),
             result.error_code),
        )
        if result.accepted:
            self.connection.execute(
                """UPDATE notification_outbox SET status='sent', sent_at=?, provider_message_id=?,
                   last_error_code=NULL, locked_at=NULL, locked_by=NULL, lock_expires_at=NULL WHERE id=?""",
                (_datetime_to_text(now), result.provider_message_id, item.id),
            )
            set_runtime_state(self.connection, "last_successful_email", _datetime_to_text(now) or "", updated_at=now)
            if item.kind == "quota_alert" and item.subscription_id is not None:
                self.connection.execute(
                    "UPDATE subscriptions SET first_provider_accepted_at=COALESCE(first_provider_accepted_at, ?) WHERE id=?",
                    (_datetime_to_text(now), item.subscription_id),
                )
        elif result.retryable and item.attempt_count <= len(RETRY_DELAYS):
            delay = RETRY_DELAYS[min(item.attempt_count - 1, len(RETRY_DELAYS) - 1)]
            self.connection.execute(
                """UPDATE notification_outbox SET status='pending', next_attempt_at=?, last_error_code=?,
                   locked_at=NULL, locked_by=NULL, lock_expires_at=NULL WHERE id=?""",
                (_datetime_to_text(now + delay), result.error_code, item.id),
            )
        else:
            self.connection.execute(
                """UPDATE notification_outbox SET status='failed', last_error_code=?,
                   locked_at=NULL, locked_by=NULL, lock_expires_at=NULL WHERE id=?""",
                (result.error_code, item.id),
            )
        self.connection.commit()
