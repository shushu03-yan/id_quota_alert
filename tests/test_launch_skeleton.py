from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import urlencode

from app.admin import list_customers, outbox_status
from app.lifecycle import create_activation_code
from app.storage import connect_database, initialize_database
from app.web import WebApplication


SECRET = "launch-skeleton-test-secret-with-more-than-32-characters"


def _request(app, path, *, method="GET", query="", form=None):
    encoded = urlencode(form or {}, doseq=True).encode()
    statuses = []
    environ = {
        "PATH_INFO": path,
        "REQUEST_METHOD": method,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(encoded)),
        "wsgi.input": BytesIO(encoded),
    }
    body = b"".join(app(environ, lambda value, headers: statuses.append(value))).decode()
    return statuses[0], body


def _app(tmp_path):
    return WebApplication(
        tmp_path / "launch.sqlite3",
        signing_secret=SECRET,
        public_base_url="https://example.test",
    )


def test_home_and_launch_policy_pages_exist(tmp_path):
    app = _app(tmp_path)
    for path in ("/", "/privacy", "/terms", "/refund", "/activate", "/manage/request"):
        status, body = _request(app, path)
        assert status == "200 OK"
        assert "HKID Alert" in body


def test_normal_activation_form_queues_verification_without_json(tmp_path):
    path = tmp_path / "launch.sqlite3"
    connection = connect_database(path)
    initialize_database(connection)
    now = datetime.now(timezone.utc)
    code = create_activation_code(connection, plan_code="trial", now=now)
    connection.close()
    app = WebApplication(path, signing_secret=SECRET, public_base_url="https://example.test")

    status, body = _request(
        app,
        "/activate",
        method="POST",
        form={
            "code": code,
            "email": "Pilot.User@example.com",
            "target_1_start": "2099-01-01",
            "target_1_end": "2099-01-10",
            "target_1_office": "*",
            "target_1_status": "limited",
        },
    )
    assert status == "202 Accepted"
    assert "Email" in body

    connection = connect_database(path)
    assert connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0
    row = connection.execute(
        "SELECT notification_kind,recipient_email,status FROM notification_outbox"
    ).fetchone()
    assert tuple(row) == ("verify_email", "pilot.user@example.com", "pending")
    connection.close()


def test_manage_request_does_not_disclose_unknown_email(tmp_path):
    app = _app(tmp_path)
    status, body = _request(
        app,
        "/manage/request",
        method="POST",
        form={"email": "nobody@example.com"},
    )
    assert status == "202 Accepted"
    assert "如果" in body
    assert "not found" not in body.lower()


def test_operator_views_mask_customer_email_and_handle_empty_outbox(tmp_path):
    connection = connect_database(tmp_path / "ops.sqlite3")
    initialize_database(connection)
    connection.execute(
        "INSERT INTO customers(email_normalized,created_at,consent_source) VALUES (?,?,?)",
        ("pilot.user@example.com", "2026-08-28T00:00:00Z", "test"),
    )
    connection.commit()
    lines = list_customers(connection)
    assert "p***@example.com" in lines[0]
    assert "pilot.user@example.com" not in lines[0]
    assert outbox_status(connection) == ["outbox empty"]
    connection.close()
