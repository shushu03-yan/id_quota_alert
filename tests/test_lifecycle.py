from datetime import datetime, timedelta, timezone

import pytest

from app.lifecycle import (
    add_subscription_recipient, begin_activation, consume_magic_link, create_activation_code, create_magic_link,
    extend_zero_match_guarantees, hash_secret, list_activation_codes, request_magic_link,
    revoke_activation_code, unsubscribe_customer, validate_targets,
    verification_token, verify_activation,
)
from app.storage import connect_database, initialize_database


NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
SECRET = "test-signing-secret-with-more-than-32-characters"
TARGET = [{"target_key": "a", "earliest_date": "2026-09-01", "deadline": "2026-09-10", "offices": ["sha-tin", "fo-tan"], "minimum_status": "limited"}]


def _db(tmp_path):
    connection = connect_database(tmp_path / "lifecycle.sqlite3")
    initialize_database(connection)
    return connection


def test_activation_code_is_random_one_time_and_only_hash_is_stored(tmp_path):
    connection = _db(tmp_path)
    code = create_activation_code(connection, plan_code="goal", now=NOW)
    row = connection.execute("SELECT code_hash,status FROM activation_codes").fetchone()
    assert len(code.split("-")) == 4
    assert row[0] == hash_secret(code)
    assert code not in row[0]
    verification_id = begin_activation(connection, code=code, email="User@Example.com", targets=TARGET,
        now=NOW, signing_secret=SECRET)
    token = verification_token(verification_id, signing_secret=SECRET)
    subscription_id = verify_activation(connection, token=token, now=NOW + timedelta(minutes=1))
    assert subscription_id > 0
    with pytest.raises(ValueError):
        verify_activation(connection, token=token, now=NOW + timedelta(minutes=2))
    assert connection.execute("SELECT status FROM activation_codes").fetchone()[0] == "redeemed"


def test_activation_code_list_hides_hash_and_revoke_is_one_way(tmp_path):
    connection = _db(tmp_path)
    create_activation_code(connection, plan_code="quick", now=NOW)
    listed = list_activation_codes(connection)
    assert "code_hash" not in listed[0].keys()
    assert revoke_activation_code(connection, listed[0]["id"])
    assert not revoke_activation_code(connection, listed[0]["id"])


def test_verification_and_magic_tokens_are_not_stored_in_plaintext(tmp_path):
    connection = _db(tmp_path)
    code = create_activation_code(connection, plan_code="goal", now=NOW)
    verification_id = begin_activation(connection, code=code, email="u@example.com", targets=TARGET,
        now=NOW, signing_secret=SECRET)
    token = verification_token(verification_id, signing_secret=SECRET)
    assert connection.execute("SELECT token_hash FROM email_verification_tokens").fetchone()[0] == hash_secret(token)
    sub_id = verify_activation(connection, token=token, now=NOW + timedelta(minutes=1))
    customer_id = connection.execute("SELECT customer_id FROM subscriptions WHERE id=?", (sub_id,)).fetchone()[0]
    _, magic = create_magic_link(connection, customer_id=customer_id, subscription_id=sub_id, now=NOW, signing_secret=SECRET)
    assert connection.execute("SELECT token_hash FROM magic_link_tokens").fetchone()[0] == hash_secret(magic)
    assert consume_magic_link(connection, token=magic, now=NOW + timedelta(minutes=1)) == (customer_id, sub_id)
    with pytest.raises(ValueError):
        consume_magic_link(connection, token=magic, now=NOW + timedelta(minutes=2))


def test_plan_target_limits_and_trial_office_rule():
    with pytest.raises(ValueError):
        validate_targets("trial", TARGET)
    too_many = [dict(TARGET[0], target_key=str(index), offices=["sha-tin"]) for index in range(4)]
    with pytest.raises(ValueError):
        validate_targets("quick", too_many)
    assert len(validate_targets("trial", [dict(TARGET[0], offices=["*"])])) == 1


@pytest.mark.parametrize("value", ["2026-9-01", "20260901", "2026-02-30", "not-a-date"])
def test_target_dates_require_real_strict_iso_dates(value):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        validate_targets("goal", [dict(TARGET[0], earliest_date=value)])


def test_target_date_range_must_not_be_reversed():
    with pytest.raises(ValueError, match="date range"):
        validate_targets(
            "goal", [dict(TARGET[0], earliest_date="2026-09-11", deadline="2026-09-10")]
        )


def test_begin_activation_rejects_fully_expired_target(tmp_path):
    connection = _db(tmp_path)
    code = create_activation_code(connection, plan_code="goal", now=NOW)
    with pytest.raises(ValueError, match="deadline has already passed"):
        begin_activation(
            connection, code=code, email="u@example.com",
            targets=[dict(TARGET[0], earliest_date="2026-08-01", deadline="2026-08-27")],
            now=NOW, signing_secret=SECRET,
        )


def test_verify_activation_rejects_target_that_expired_while_waiting(tmp_path):
    connection = _db(tmp_path)
    late_hong_kong_evening = datetime(2026, 8, 28, 15, 59, tzinfo=timezone.utc)
    code = create_activation_code(connection, plan_code="goal", now=late_hong_kong_evening)
    verification_id = begin_activation(
        connection, code=code, email="u@example.com",
        targets=[dict(TARGET[0], earliest_date="2026-08-28", deadline="2026-08-28")],
        now=late_hong_kong_evening, signing_secret=SECRET,
    )
    with pytest.raises(ValueError, match="deadline has already passed"):
        verify_activation(
            connection, token=verification_token(verification_id, signing_secret=SECRET),
            now=late_hong_kong_evening + timedelta(minutes=2),
        )
    assert connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0


def test_two_connections_cannot_reserve_same_code_for_different_customers(tmp_path):
    path = tmp_path / "reservation-race.sqlite3"
    first = connect_database(path)
    initialize_database(first)
    code = create_activation_code(first, plan_code="goal", now=NOW)
    second = connect_database(path)
    initialize_database(second)

    begin_activation(
        first, code=code, email="one@example.com", targets=TARGET, now=NOW,
        signing_secret=SECRET,
    )
    with pytest.raises(ValueError, match="reserved"):
        begin_activation(
            second, code=code, email="two@example.com", targets=TARGET, now=NOW,
            signing_secret=SECRET,
        )
    owner = second.execute("""SELECT c.email_normalized FROM activation_codes a
        JOIN customers c ON c.id=a.reserved_customer_id""").fetchone()[0]
    assert owner == "one@example.com"


def test_expired_reservation_can_be_reacquired_and_old_owner_cannot_verify(tmp_path):
    path = tmp_path / "reservation-expiry.sqlite3"
    first = connect_database(path)
    initialize_database(first)
    code = create_activation_code(first, plan_code="goal", now=NOW)
    first_id = begin_activation(
        first, code=code, email="one@example.com", targets=TARGET, now=NOW,
        signing_secret=SECRET, verification_ttl=timedelta(minutes=1),
    )
    second = connect_database(path)
    initialize_database(second)
    second_id = begin_activation(
        second, code=code, email="two@example.com", targets=TARGET, now=NOW + timedelta(minutes=2),
        signing_secret=SECRET,
    )
    with pytest.raises(ValueError, match="does not own"):
        verify_activation(
            first, token=verification_token(first_id, signing_secret=SECRET),
            now=NOW + timedelta(seconds=30),
        )
    assert verify_activation(
        second, token=verification_token(second_id, signing_secret=SECRET),
        now=NOW + timedelta(minutes=3),
    ) > 0


def test_trial_can_only_be_redeemed_once_per_email(tmp_path):
    connection = _db(tmp_path)
    first = create_activation_code(connection, plan_code="trial", now=NOW)
    target = [dict(TARGET[0], offices=["sha-tin"])]
    verification_id = begin_activation(connection, code=first, email="u@example.com", targets=target,
        now=NOW, signing_secret=SECRET)
    verify_activation(connection, token=verification_token(verification_id, signing_secret=SECRET), now=NOW + timedelta(minutes=1))
    second = create_activation_code(connection, plan_code="trial", now=NOW)
    with pytest.raises(ValueError, match="trial already used"):
        begin_activation(connection, code=second, email="U@example.com", targets=target,
            now=NOW, signing_secret=SECRET)


def test_activation_queues_structured_template_data_and_preserves_target_group(tmp_path):
    connection = _db(tmp_path)
    code = create_activation_code(connection, plan_code="goal", now=NOW)
    verification_id = begin_activation(connection, code=code, email="u@example.com", targets=TARGET,
        now=NOW, signing_secret=SECRET)
    sub_id = verify_activation(connection, token=verification_token(verification_id, signing_secret=SECRET), now=NOW + timedelta(minutes=1))
    rows = connection.execute("SELECT target_key,office_id FROM subscription_filters WHERE subscription_id=?", (sub_id,)).fetchall()
    assert {tuple(row) for row in rows} == {("a", "sha-tin"), ("a", "fo-tan")}
    outbox = connection.execute("SELECT notification_kind,payload_json FROM notification_outbox WHERE subscription_id=?", (sub_id,)).fetchone()
    assert outbox[0] == "activation_test"
    assert '"plan_name":"Goal"' in outbox[1]
    assert '"target_count":1' in outbox[1]


def test_zero_match_guarantee_extends_goal_once_only(tmp_path):
    connection = _db(tmp_path)
    customer_id = connection.execute("INSERT INTO customers(email_normalized,created_at,consent_source) VALUES ('u@example.com','2026-08-01T00:00:00Z','test')").lastrowid
    sub_id = connection.execute("""INSERT INTO subscriptions(customer_id,plan_code,starts_at,activated_at,original_expires_at,expires_at,active,created_at)
        VALUES (?,'goal','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z','2026-08-15T00:00:00Z','2026-08-15T00:00:00Z',1,'2026-08-01T00:00:00Z')""", (customer_id,)).lastrowid
    connection.execute("""INSERT INTO subscription_filters(
        subscription_id,target_key,earliest_date,deadline,office_id,minimum_status)
        VALUES (?,'a','2026-08-01','2026-09-10','sha-tin','limited')""", (sub_id,))
    connection.commit()
    assert extend_zero_match_guarantees(connection, now=NOW) == 1
    assert extend_zero_match_guarantees(connection, now=NOW + timedelta(days=1)) == 0
    row = connection.execute("SELECT expires_at,guarantee_extended_at FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
    assert row[0] == "2026-08-22T00:00:00Z" and row[1] == "2026-08-28T12:00:00Z"


def test_matching_or_ineligible_plan_never_extends(tmp_path):
    connection = _db(tmp_path)
    customer_id = connection.execute("INSERT INTO customers(email_normalized,created_at,consent_source) VALUES ('u@example.com','2026-08-01T00:00:00Z','test')").lastrowid
    for plan, match in (("goal", "2026-08-02T00:00:00Z"), ("quick", None)):
        connection.execute("""INSERT INTO subscriptions(customer_id,plan_code,starts_at,activated_at,original_expires_at,expires_at,active,first_matched_event_at,created_at)
            VALUES (?,?, '2026-08-01T00:00:00Z','2026-08-01T00:00:00Z','2026-08-15T00:00:00Z','2026-08-15T00:00:00Z',1,?,'2026-08-01T00:00:00Z')""", (customer_id, plan, match))
    connection.commit()
    assert extend_zero_match_guarantees(connection, now=NOW) == 0


@pytest.mark.parametrize(
    ("earliest", "deadline"),
    [("bad-date", "2026-09-10"), ("2026-08-01", "2026-08-27"), ("2026-09-11", "2026-09-10")],
)
def test_invalid_or_expired_targets_never_receive_guarantee(tmp_path, earliest, deadline):
    connection = _db(tmp_path)
    customer_id = connection.execute(
        "INSERT INTO customers(email_normalized,created_at,consent_source) VALUES ('u@example.com','2026-08-01T00:00:00Z','test')"
    ).lastrowid
    sub_id = connection.execute("""INSERT INTO subscriptions(
        customer_id,plan_code,starts_at,activated_at,original_expires_at,expires_at,active,created_at)
        VALUES (?,'goal','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z',
                '2026-08-15T00:00:00Z','2026-08-15T00:00:00Z',1,'2026-08-01T00:00:00Z')""",
        (customer_id,),
    ).lastrowid
    connection.execute("""INSERT INTO subscription_filters(
        subscription_id,target_key,earliest_date,deadline,office_id,minimum_status)
        VALUES (?,'a',?,?,'sha-tin','limited')""", (sub_id, earliest, deadline))
    connection.commit()
    assert extend_zero_match_guarantees(connection, now=NOW) == 0
    assert connection.execute(
        "SELECT guarantee_extended_at FROM subscriptions WHERE id=?", (sub_id,)
    ).fetchone()[0] is None


def test_unsubscribe_deactivates_and_cancels_pending_quota_mail(tmp_path):
    connection = _db(tmp_path)
    code = create_activation_code(connection, plan_code="goal", now=NOW)
    vid = begin_activation(connection, code=code, email="u@example.com", targets=TARGET, now=NOW,
                           signing_secret=SECRET)
    sub_id = verify_activation(connection, token=verification_token(vid, signing_secret=SECRET), now=NOW + timedelta(minutes=1))
    customer_id = connection.execute("SELECT customer_id FROM subscriptions WHERE id=?", (sub_id,)).fetchone()[0]
    connection.execute("UPDATE notification_outbox SET notification_kind='quota_alert' WHERE subscription_id=?", (sub_id,))
    connection.commit()
    unsubscribe_customer(connection, customer_id=customer_id, now=NOW + timedelta(minutes=2))
    assert connection.execute("SELECT active FROM subscriptions WHERE id=?", (sub_id,)).fetchone()[0] == 0
    assert connection.execute("SELECT status FROM notification_outbox WHERE subscription_id=?", (sub_id,)).fetchone()[0] == "cancelled"


def test_family_supports_three_recipients_but_other_plans_do_not(tmp_path):
    connection = _db(tmp_path)
    code = create_activation_code(connection, plan_code="family", now=NOW)
    vid = begin_activation(connection, code=code, email="one@example.com", targets=TARGET, now=NOW,
                           signing_secret=SECRET)
    sub_id = verify_activation(connection, token=verification_token(vid, signing_secret=SECRET), now=NOW + timedelta(minutes=1))
    assert add_subscription_recipient(connection, subscription_id=sub_id, email="two@example.com", now=NOW)
    assert add_subscription_recipient(connection, subscription_id=sub_id, email="three@example.com", now=NOW)
    with pytest.raises(ValueError, match="recipient count"):
        add_subscription_recipient(connection, subscription_id=sub_id, email="four@example.com", now=NOW)


def test_manage_link_email_queues_only_record_id_not_plain_token(tmp_path):
    connection = _db(tmp_path)
    code = create_activation_code(connection, plan_code="goal", now=NOW)
    vid = begin_activation(connection, code=code, email="u@example.com", targets=TARGET, now=NOW,
                           signing_secret=SECRET)
    verify_activation(connection, token=verification_token(vid, signing_secret=SECRET), now=NOW + timedelta(minutes=1))
    token_id = request_magic_link(connection, email="u@example.com", now=NOW + timedelta(minutes=2),
                                  signing_secret=SECRET)
    payload = connection.execute("SELECT payload_json FROM notification_outbox WHERE notification_kind='manage_link'").fetchone()[0]
    assert f'"magic_link_id":{token_id}' in payload
    assert "token=" not in payload
    assert "http" not in payload


def test_expired_subscription_cannot_request_management_link(tmp_path):
    connection = _db(tmp_path)
    code = create_activation_code(connection, plan_code="goal", now=NOW)
    verification_id = begin_activation(
        connection, code=code, email="u@example.com", targets=TARGET,
        now=NOW, signing_secret=SECRET,
    )
    subscription_id = verify_activation(
        connection,
        token=verification_token(verification_id, signing_secret=SECRET),
        now=NOW + timedelta(minutes=1),
    )
    connection.execute(
        "UPDATE subscriptions SET expires_at=? WHERE id=?",
        ((NOW + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"), subscription_id),
    )
    connection.commit()
    with pytest.raises(ValueError, match="active subscription not found"):
        request_magic_link(
            connection, email="u@example.com", now=NOW + timedelta(minutes=3),
            signing_secret=SECRET,
        )
