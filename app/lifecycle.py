"""Plan rules and privacy-preserving activation, verification and management flows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from typing import Iterable

from .notifications import queue_notification
from .storage import _datetime_from_text, _datetime_to_text, initialize_database


@dataclass(frozen=True, slots=True)
class Plan:
    code: str
    duration: timedelta
    max_targets: int
    max_emails: int
    guarantee_extension: timedelta | None = None


PLANS = {
    "trial": Plan("trial", timedelta(hours=24), 1, 1),
    "quick": Plan("quick", timedelta(days=3), 3, 1),
    "goal": Plan("goal", timedelta(days=14), 6, 1, timedelta(days=7)),
    "family": Plan("family", timedelta(days=14), 10, 3, timedelta(days=7)),
}
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def normalize_email(email: str) -> str:
    normalized = email.strip().casefold()
    if normalized.count("@") != 1 or normalized.startswith("@") or normalized.endswith("@") or len(normalized) > 254:
        raise ValueError("invalid email address")
    return normalized


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_activation_code(
    connection: sqlite3.Connection, *, plan_code: str, now: datetime,
    expires_in: timedelta = timedelta(days=30), order_reference: str | None = None,
) -> str:
    initialize_database(connection)
    if plan_code not in PLANS:
        raise ValueError("unknown plan")
    while True:
        raw = "-".join("".join(secrets.choice(CODE_ALPHABET) for _ in range(4)) for _ in range(4))
        try:
            connection.execute(
                """INSERT INTO activation_codes(code_hash, plan_code, created_at, expires_at, status, order_reference)
                   VALUES (?, ?, ?, ?, 'available', ?)""",
                (hash_secret(raw), plan_code, _datetime_to_text(now), _datetime_to_text(now + expires_in), order_reference),
            )
            connection.commit()
            return raw
        except sqlite3.IntegrityError:
            continue


def list_activation_codes(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    initialize_database(connection)
    return connection.execute(
        "SELECT id, plan_code, created_at, expires_at, status, redeemed_at, order_reference FROM activation_codes ORDER BY id"
    ).fetchall()


def revoke_activation_code(connection: sqlite3.Connection, code_id: int) -> bool:
    initialize_database(connection)
    cursor = connection.execute(
        "UPDATE activation_codes SET status='revoked' WHERE id=? AND status IN ('available', 'reserved')", (code_id,)
    )
    connection.commit()
    return cursor.rowcount == 1


def validate_targets(plan_code: str, targets: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    plan = PLANS[plan_code]
    normalized = list(targets)
    keys = {str(target.get("target_key", "")) for target in normalized}
    if not normalized or "" in keys or len(keys) != len(normalized) or len(keys) > plan.max_targets:
        raise ValueError("target count or target_key violates plan limit")
    for target in normalized:
        earliest, deadline = str(target.get("earliest_date", "")), str(target.get("deadline", ""))
        offices = target.get("offices")
        minimum = str(target.get("minimum_status", ""))
        if not earliest or not deadline or earliest > deadline:
            raise ValueError("invalid target date range")
        if not isinstance(offices, list) or not offices or not all(isinstance(x, str) and x for x in offices):
            raise ValueError("target requires one or more offices")
        if "*" in offices and len(offices) != 1:
            raise ValueError("all offices cannot be combined with specific offices")
        if plan_code == "trial" and len(offices) != 1:
            raise ValueError("trial allows one office or all offices")
        if minimum not in {"limited", "available"}:
            raise ValueError("invalid minimum status")
    return normalized


def _signed_token(record_id: int, *, purpose: str, secret: str) -> str:
    identifier = base64.urlsafe_b64encode(str(record_id).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), f"{purpose}:{identifier}".encode(), hashlib.sha256).digest()
    return f"{identifier}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def begin_activation(
    connection: sqlite3.Connection, *, code: str, email: str,
    targets: Iterable[dict[str, object]], now: datetime, base_url: str,
    signing_secret: str, verification_ttl: timedelta = timedelta(hours=1),
) -> int:
    """Reserve a code and queue verification; neither code nor token plaintext is persisted."""
    initialize_database(connection)
    normalized_email = normalize_email(email)
    row = connection.execute("SELECT * FROM activation_codes WHERE code_hash=?", (hash_secret(code.upper()),)).fetchone()
    if row is None or row["status"] not in {"available", "reserved"}:
        raise ValueError("activation code unavailable")
    if (_datetime_from_text(row["expires_at"]) or now) <= now:
        connection.execute("UPDATE activation_codes SET status='expired' WHERE id=?", (row["id"],))
        connection.commit()
        raise ValueError("activation code expired")
    plan_code = str(row["plan_code"])
    normalized_targets = validate_targets(plan_code, targets)
    customer = connection.execute("SELECT * FROM customers WHERE email_normalized=?", (normalized_email,)).fetchone()
    if customer is not None and plan_code == "trial" and customer["trial_used_at"] is not None:
        raise ValueError("trial already used for this email")
    if customer is None:
        customer_id = int(connection.execute(
            "INSERT INTO customers(email_normalized, created_at, consent_source) VALUES (?, ?, 'activation')",
            (normalized_email, _datetime_to_text(now)),
        ).lastrowid)
    else:
        customer_id = int(customer["id"])
    reservation_expiry = now + verification_ttl
    if row["status"] == "reserved" and row["reserved_customer_id"] != customer_id and (_datetime_from_text(row["reservation_expires_at"]) or now) > now:
        raise ValueError("activation code is reserved")
    connection.execute(
        """UPDATE activation_codes SET status='reserved', reserved_at=?, reservation_expires_at=?,
           reserved_customer_id=? WHERE id=?""",
        (_datetime_to_text(now), _datetime_to_text(reservation_expiry), customer_id, row["id"]),
    )
    cursor = connection.execute(
        """INSERT INTO email_verification_tokens(token_hash, activation_code_id, customer_id,
           targets_json, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)""",
        (f"pending:{secrets.token_hex(16)}", row["id"], customer_id, json.dumps(normalized_targets, separators=(",", ":")),
         _datetime_to_text(now), _datetime_to_text(reservation_expiry)),
    )
    verification_id = int(cursor.lastrowid)
    token = _signed_token(verification_id, purpose="verify", secret=signing_secret)
    connection.execute("UPDATE email_verification_tokens SET token_hash=? WHERE id=?", (hash_secret(token), verification_id))
    queue_notification(
        connection, notification_kind="verify_email", dedup_key=f"verify_email:{verification_id}",
        recipient_email=normalized_email,
        payload={"base_url": base_url.rstrip("/"), "verification_id": verification_id}, created_at=now,
    )
    connection.commit()
    return verification_id


def verification_token(verification_id: int, *, signing_secret: str) -> str:
    """Reconstruct a signed URL token without storing it in plaintext."""
    return _signed_token(verification_id, purpose="verify", secret=signing_secret)


def verify_activation(connection: sqlite3.Connection, *, token: str, now: datetime) -> int:
    initialize_database(connection)
    row = connection.execute(
        """SELECT v.*, a.plan_code, a.status AS code_status, a.expires_at AS code_expires_at
           FROM email_verification_tokens v JOIN activation_codes a ON a.id=v.activation_code_id
           WHERE v.token_hash=?""", (hash_secret(token),),
    ).fetchone()
    if row is None or row["used_at"] is not None or row["code_status"] != "reserved":
        raise ValueError("verification token unavailable")
    if (_datetime_from_text(row["expires_at"]) or now) <= now or (_datetime_from_text(row["code_expires_at"]) or now) <= now:
        raise ValueError("verification token expired")
    plan = PLANS[str(row["plan_code"])]
    expires_at = now + plan.duration
    cursor = connection.execute(
        """INSERT INTO subscriptions(customer_id, plan_code, starts_at, activated_at,
           original_expires_at, expires_at, active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (row["customer_id"], plan.code, _datetime_to_text(now), _datetime_to_text(now),
         _datetime_to_text(expires_at), _datetime_to_text(expires_at), _datetime_to_text(now)),
    )
    subscription_id = int(cursor.lastrowid)
    targets = json.loads(row["targets_json"])
    for target in targets:
        for office in target["offices"]:
            connection.execute(
                """INSERT INTO subscription_filters(subscription_id, target_key, earliest_date,
                   deadline, office_id, minimum_status) VALUES (?, ?, ?, ?, ?, ?)""",
                (subscription_id, target["target_key"], target["earliest_date"], target["deadline"], office, target["minimum_status"]),
            )
    connection.execute(
        "INSERT INTO subscription_recipients(subscription_id, email_normalized, created_at) SELECT ?, email_normalized, ? FROM customers WHERE id=?",
        (subscription_id, _datetime_to_text(now), row["customer_id"]),
    )
    connection.execute("UPDATE email_verification_tokens SET used_at=? WHERE id=?", (_datetime_to_text(now), row["id"]))
    connection.execute(
        """UPDATE activation_codes SET status='redeemed', redeemed_at=?, redeemed_customer_id=?,
           reserved_at=NULL, reservation_expires_at=NULL WHERE id=?""",
        (_datetime_to_text(now), row["customer_id"], row["activation_code_id"]),
    )
    if plan.code == "trial":
        connection.execute("UPDATE customers SET trial_used_at=COALESCE(trial_used_at, ?) WHERE id=?", (_datetime_to_text(now), row["customer_id"]))
    customer = connection.execute("SELECT email_normalized FROM customers WHERE id=?", (row["customer_id"],)).fetchone()
    message = (f"你的 HKID Alert 已成功启动\n\n套餐：{plan.code.title()}\n"
               f"有效期：{now.date().isoformat()} ～ {expires_at.date().isoformat()}\n"
               f"预约目标：已设置 {len(targets)} / {plan.max_targets}\n\n"
               "这是一封服务启动确认邮件，不是实际配额提醒，不代表现在有预约名额。\n"
               "后续检测到符合目标的公开名额变化时会发送提醒。\n"
               "本服务并非香港政府官方服务；收到提醒后仍需自行进入官方系统预约。")
    queue_notification(
        connection, notification_kind="activation_test", dedup_key=f"activation_test:{subscription_id}",
        recipient_email=str(customer[0]), payload={"message": message}, created_at=now,
        subscription_id=subscription_id,
    )
    connection.commit()
    return subscription_id


def extend_zero_match_guarantees(connection: sqlite3.Connection, *, now: datetime) -> int:
    initialize_database(connection)
    extended = 0
    for row in connection.execute(
        """SELECT id, plan_code, expires_at FROM subscriptions WHERE active=1
           AND plan_code IN ('goal','family') AND original_expires_at <= ?
           AND first_matched_event_at IS NULL AND guarantee_extended_at IS NULL""",
        (_datetime_to_text(now),),
    ).fetchall():
        new_expiry = (_datetime_from_text(row["expires_at"]) or now) + PLANS[row["plan_code"]].guarantee_extension  # type: ignore[operator]
        connection.execute("UPDATE subscriptions SET expires_at=?, guarantee_extended_at=? WHERE id=?",
                           (_datetime_to_text(new_expiry), _datetime_to_text(now), row["id"]))
        extended += 1
    connection.commit()
    return extended


def add_subscription_recipient(
    connection: sqlite3.Connection, *, subscription_id: int, email: str, now: datetime,
) -> bool:
    """Add a recipient within the plan cap (Family supports up to three)."""
    initialize_database(connection)
    row = connection.execute("SELECT plan_code FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
    if row is None:
        raise ValueError("subscription does not exist")
    normalized = normalize_email(email)
    existing = connection.execute(
        "SELECT COUNT(*) FROM subscription_recipients WHERE subscription_id=?", (subscription_id,)
    ).fetchone()[0]
    duplicate = connection.execute(
        "SELECT 1 FROM subscription_recipients WHERE subscription_id=? AND email_normalized=?",
        (subscription_id, normalized),
    ).fetchone()
    if duplicate:
        return False
    if existing >= PLANS[str(row["plan_code"])].max_emails:
        raise ValueError("recipient count violates plan limit")
    connection.execute(
        "INSERT INTO subscription_recipients(subscription_id,email_normalized,created_at) VALUES (?,?,?)",
        (subscription_id, normalized, _datetime_to_text(now)),
    )
    connection.commit()
    return True


def create_magic_link(
    connection: sqlite3.Connection, *, customer_id: int, subscription_id: int | None,
    now: datetime, signing_secret: str, ttl: timedelta = timedelta(minutes=20),
) -> tuple[int, str]:
    initialize_database(connection)
    cursor = connection.execute(
        """INSERT INTO magic_link_tokens(token_hash, customer_id, subscription_id, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?)""",
        (f"pending:{secrets.token_hex(16)}", customer_id, subscription_id, _datetime_to_text(now), _datetime_to_text(now + ttl)),
    )
    token_id = int(cursor.lastrowid)
    token = _signed_token(token_id, purpose="manage", secret=signing_secret)
    connection.execute("UPDATE magic_link_tokens SET token_hash=? WHERE id=?", (hash_secret(token), token_id))
    connection.commit()
    return token_id, token


def consume_magic_link(connection: sqlite3.Connection, *, token: str, now: datetime) -> tuple[int, int | None]:
    row = connection.execute("SELECT * FROM magic_link_tokens WHERE token_hash=?", (hash_secret(token),)).fetchone()
    if row is None or row["used_at"] is not None or (_datetime_from_text(row["expires_at"]) or now) <= now:
        raise ValueError("magic link unavailable")
    connection.execute("UPDATE magic_link_tokens SET used_at=? WHERE id=?", (_datetime_to_text(now), row["id"]))
    connection.commit()
    return int(row["customer_id"]), None if row["subscription_id"] is None else int(row["subscription_id"])


def request_magic_link(
    connection: sqlite3.Connection, *, email: str, now: datetime,
    signing_secret: str, base_url: str,
) -> int:
    """Queue a management link while persisting only its deterministic hash."""
    initialize_database(connection)
    normalized = normalize_email(email)
    row = connection.execute(
        """SELECT c.id AS customer_id, s.id AS subscription_id FROM customers c
           JOIN subscriptions s ON s.customer_id=c.id
           WHERE c.email_normalized=? AND c.unsubscribed_at IS NULL AND s.active=1
           ORDER BY s.id DESC LIMIT 1""", (normalized,),
    ).fetchone()
    if row is None:
        raise ValueError("active subscription not found")
    token_id, _ = create_magic_link(connection, customer_id=int(row["customer_id"]),
        subscription_id=int(row["subscription_id"]), now=now, signing_secret=signing_secret)
    queue_notification(
        connection, notification_kind="manage_link", dedup_key=f"manage_link:{token_id}",
        recipient_email=normalized, payload={"base_url": base_url.rstrip("/"), "magic_link_id": token_id},
        created_at=now, subscription_id=int(row["subscription_id"]),
    )
    connection.commit()
    return token_id


def unsubscribe_customer(connection: sqlite3.Connection, *, customer_id: int, now: datetime) -> None:
    initialize_database(connection)
    connection.execute("UPDATE customers SET unsubscribed_at=COALESCE(unsubscribed_at, ?) WHERE id=?", (_datetime_to_text(now), customer_id))
    connection.execute("UPDATE subscriptions SET active=0 WHERE customer_id=?", (customer_id,))
    connection.execute(
        """UPDATE notification_outbox SET status='cancelled' WHERE notification_kind='quota_alert'
           AND status='pending' AND subscription_id IN (SELECT id FROM subscriptions WHERE customer_id=?)""", (customer_id,)
    )
    connection.commit()
