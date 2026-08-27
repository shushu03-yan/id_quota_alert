from datetime import datetime, timedelta, timezone
import sqlite3

from app.email_provider import EmailDeliveryResult
from app.notifications import EmailWorker, claim_notification, match_quota_event, queue_notification
from app.storage import connect_database, initialize_database


NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)


class Provider:
    def __init__(self, result):
        self.result = result
        self.calls = 0
    def send(self, **kwargs):
        self.calls += 1
        return self.result


def _db(tmp_path):
    connection = connect_database(tmp_path / "notify.sqlite3")
    initialize_database(connection)
    return connection


def _subscription_event(connection):
    customer_id = connection.execute("INSERT INTO customers(email_normalized,created_at,consent_source) VALUES ('u@example.com',?,'test')", ("2026-08-28T10:00:00Z",)).lastrowid
    sub_id = connection.execute("""INSERT INTO subscriptions(customer_id,plan_code,starts_at,activated_at,original_expires_at,expires_at,active,created_at)
        VALUES (?,'goal','2026-08-28T10:00:00Z','2026-08-28T10:00:00Z','2026-09-11T10:00:00Z','2026-09-11T10:00:00Z',1,'2026-08-28T10:00:00Z')""", (customer_id,)).lastrowid
    connection.execute("INSERT INTO subscription_filters(subscription_id,target_key,earliest_date,deadline,office_id,minimum_status) VALUES (?,'a','2026-09-01','2026-09-10','sha-tin','limited')", (sub_id,))
    event_id = connection.execute("""INSERT INTO quota_events(quota_date,office_id,from_status,to_status,occurrence_id,observed_at,created_at)
        VALUES ('2026-09-03','sha-tin','unavailable','available','occ','2026-08-28T12:00:00Z','2026-08-28T12:00:00Z')""").lastrowid
    connection.commit()
    return customer_id, sub_id, event_id


def test_matcher_queues_once_and_sets_first_metrics(tmp_path):
    connection = _db(tmp_path)
    _, sub_id, event_id = _subscription_event(connection)
    assert match_quota_event(connection, event_id, now=NOW) == 1
    assert match_quota_event(connection, event_id, now=NOW + timedelta(minutes=1)) == 0
    row = connection.execute("SELECT first_matched_event_at,first_notification_queued_at FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
    assert row[0] == row[1] == "2026-08-28T12:00:00Z"
    assert connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1


def test_email_worker_success_saves_provider_id_attempt_and_first_acceptance(tmp_path):
    connection = _db(tmp_path)
    _, sub_id, event_id = _subscription_event(connection)
    match_quota_event(connection, event_id, now=NOW)
    provider = Provider(EmailDeliveryResult(True, "provider-1"))
    assert EmailWorker(connection, provider, worker_id="w").run_once(now=NOW)
    row = connection.execute("SELECT status,provider_message_id FROM notification_outbox").fetchone()
    assert tuple(row) == ("sent", "provider-1")
    assert connection.execute("SELECT result,provider_message_id FROM delivery_attempts").fetchone()[0] == "accepted"
    assert connection.execute("SELECT first_provider_accepted_at FROM subscriptions WHERE id=?", (sub_id,)).fetchone()[0] == "2026-08-28T12:00:00Z"


def test_retryable_failure_is_delayed_and_audited(tmp_path):
    connection = _db(tmp_path)
    queue_notification(connection, notification_kind="activation_test", dedup_key="x", recipient_email="u@example.com", payload={}, created_at=NOW)
    connection.commit()
    provider = Provider(EmailDeliveryResult(False, retryable=True, error_code="temporary"))
    EmailWorker(connection, provider, worker_id="w").run_once(now=NOW)
    row = connection.execute("SELECT status,next_attempt_at,last_error_code FROM notification_outbox").fetchone()
    assert row[0] == "pending" and row[1] == "2026-08-28T12:01:00Z" and row[2] == "temporary"
    assert connection.execute("SELECT result FROM delivery_attempts").fetchone()[0] == "retryable_failure"


def test_retry_schedule_is_bounded_1_5_15_60_minutes(tmp_path):
    connection = _db(tmp_path)
    queue_notification(connection, notification_kind="activation_test", dedup_key="retry", recipient_email="u@example.com", payload={}, created_at=NOW)
    connection.commit()
    worker = EmailWorker(connection, Provider(EmailDeliveryResult(False, retryable=True, error_code="temporary")), worker_id="w")
    moments = [NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=6), NOW + timedelta(minutes=21)]
    expected = [NOW + timedelta(minutes=1), NOW + timedelta(minutes=6), NOW + timedelta(minutes=21), NOW + timedelta(minutes=81)]
    for moment, next_at in zip(moments, expected):
        assert worker.run_once(now=moment)
        assert connection.execute("SELECT next_attempt_at FROM notification_outbox").fetchone()[0] == next_at.isoformat().replace("+00:00", "Z")
    assert worker.run_once(now=expected[-1])
    assert connection.execute("SELECT status FROM notification_outbox").fetchone()[0] == "failed"


def test_permanent_failure_is_not_reclaimed(tmp_path):
    connection = _db(tmp_path)
    queue_notification(connection, notification_kind="activation_test", dedup_key="x", recipient_email="u@example.com", payload={}, created_at=NOW)
    connection.commit()
    provider = Provider(EmailDeliveryResult(False, retryable=False, error_code="bad_address"))
    worker = EmailWorker(connection, provider, worker_id="w")
    assert worker.run_once(now=NOW)
    assert not worker.run_once(now=NOW + timedelta(days=1))
    assert provider.calls == 1
    assert connection.execute("SELECT status FROM notification_outbox").fetchone()[0] == "failed"


def test_expired_sending_lease_is_recovered(tmp_path):
    connection = _db(tmp_path)
    outbox_id, _ = queue_notification(connection, notification_kind="activation_test", dedup_key="x", recipient_email="u@example.com", payload={}, created_at=NOW)
    connection.commit()
    first = claim_notification(connection, worker_id="crashed", now=NOW, lease_duration=timedelta(minutes=1))
    assert first and first.id == outbox_id
    recovered = claim_notification(connection, worker_id="recovery", now=NOW + timedelta(minutes=2))
    assert recovered and recovered.id == outbox_id and recovered.attempt_count == 2


def test_unexpired_lease_prevents_competing_worker_claim(tmp_path):
    connection = _db(tmp_path)
    queue_notification(connection, notification_kind="activation_test", dedup_key="x", recipient_email="u@example.com", payload={}, created_at=NOW)
    connection.commit()
    assert claim_notification(connection, worker_id="one", now=NOW)
    assert claim_notification(connection, worker_id="two", now=NOW + timedelta(minutes=1)) is None


def test_sent_message_is_never_claimed_again(tmp_path):
    connection = _db(tmp_path)
    queue_notification(connection, notification_kind="activation_test", dedup_key="x", recipient_email="u@example.com", payload={}, created_at=NOW)
    connection.commit()
    worker = EmailWorker(connection, Provider(EmailDeliveryResult(True)), worker_id="w")
    worker.run_once(now=NOW)
    assert claim_notification(connection, worker_id="other", now=NOW + timedelta(days=1)) is None


def test_unsubscribed_customer_does_not_get_new_match(tmp_path):
    connection = _db(tmp_path)
    customer_id, _, event_id = _subscription_event(connection)
    connection.execute("UPDATE customers SET unsubscribed_at=? WHERE id=?", ("2026-08-28T11:00:00Z", customer_id))
    connection.commit()
    assert match_quota_event(connection, event_id, now=NOW) == 0


def test_dedup_key_is_database_unique(tmp_path):
    connection = _db(tmp_path)
    queue_notification(connection, notification_kind="activation_test", dedup_key="same", recipient_email="u@example.com", payload={}, created_at=NOW)
    _, created = queue_notification(connection, notification_kind="activation_test", dedup_key="same", recipient_email="u@example.com", payload={}, created_at=NOW)
    assert created is False


def test_matcher_queues_each_configured_family_recipient(tmp_path):
    connection = _db(tmp_path)
    _, sub_id, event_id = _subscription_event(connection)
    connection.execute("INSERT INTO subscription_recipients(subscription_id,email_normalized,created_at) VALUES (?,'one@example.com','2026-08-28T10:00:00Z')", (sub_id,))
    connection.execute("INSERT INTO subscription_recipients(subscription_id,email_normalized,created_at) VALUES (?,'two@example.com','2026-08-28T10:00:00Z')", (sub_id,))
    connection.commit()
    assert match_quota_event(connection, event_id, now=NOW) == 2
    assert {row[0] for row in connection.execute("SELECT recipient_email FROM notification_outbox")} == {"one@example.com", "two@example.com"}
