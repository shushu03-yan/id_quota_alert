from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urlencode

import pytest

from app.config import TOKEN_SIGNING_SECRET_PLACEHOLDER
from app.lifecycle import (
    begin_activation, create_activation_code, create_magic_link, verification_token, verify_activation,
)
from app.storage import connect_database, initialize_database
from app.web import WebApplication


NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
SECRET = "web-test-secret-with-at-least-thirty-two-characters"
TARGET = [{"target_key":"a","earliest_date":"2026-09-01","deadline":"2026-09-10","offices":["sha-tin"],"minimum_status":"limited"}]


def _request(app, path, *, method="GET", query="", form=None):
    encoded = urlencode(form or {}).encode()
    status = []
    environ = {"PATH_INFO": path, "REQUEST_METHOD": method, "QUERY_STRING": query,
               "CONTENT_LENGTH": str(len(encoded)), "wsgi.input": BytesIO(encoded)}
    body = b"".join(app(environ, lambda value, headers: status.append(value)))
    return status[0], body.decode()


def test_activation_verify_manage_and_unsubscribe_routes(tmp_path):
    path = tmp_path / "web.sqlite3"
    connection = connect_database(path)
    initialize_database(connection)
    code = create_activation_code(connection, plan_code="trial", now=NOW)
    vid = begin_activation(connection, code=code, email="u@example.com", targets=TARGET,
                           now=NOW, base_url="https://example.test", signing_secret=SECRET)
    token = verification_token(vid, signing_secret=SECRET)
    app = WebApplication(
        path, signing_secret=SECRET, public_base_url="https://example.test",
        clock=lambda: NOW + timedelta(minutes=1),
    )
    assert _request(app, "/activate")[0] == "200 OK"
    assert _request(app, "/verify", query=urlencode({"token": token}))[0] == "200 OK"
    sub = connection.execute("SELECT id,customer_id FROM subscriptions").fetchone()
    _, magic = create_magic_link(connection, customer_id=sub["customer_id"], subscription_id=sub["id"], now=NOW, signing_secret=SECRET)
    assert _request(app, "/manage", query=urlencode({"token": magic}))[0] == "200 OK"
    assert _request(app, "/unsubscribe", query=urlencode({"token": magic}))[0] == "200 OK"
    assert _request(app, "/unsubscribe", method="POST", form={"token": magic})[0] == "200 OK"
    assert connection.execute("SELECT active FROM subscriptions").fetchone()[0] == 0


def test_magic_link_target_update_uses_strict_date_validation(tmp_path):
    path = tmp_path / "web-targets.sqlite3"
    connection = connect_database(path)
    initialize_database(connection)
    code = create_activation_code(connection, plan_code="goal", now=NOW)
    vid = begin_activation(
        connection, code=code, email="u@example.com", targets=TARGET,
        now=NOW, base_url="https://example.test", signing_secret=SECRET,
    )
    sub_id = verify_activation(
        connection, token=verification_token(vid, signing_secret=SECRET), now=NOW + timedelta(minutes=1)
    )
    customer_id = connection.execute(
        "SELECT customer_id FROM subscriptions WHERE id=?", (sub_id,)
    ).fetchone()[0]
    _, magic = create_magic_link(
        connection, customer_id=customer_id, subscription_id=sub_id, now=NOW,
        signing_secret=SECRET,
    )
    app = WebApplication(
        path, signing_secret=SECRET, public_base_url="https://example.test",
        clock=lambda: NOW + timedelta(minutes=1),
    )
    status, body = _request(
        app, "/manage", method="POST",
        form={"token": magic, "targets": '[{"target_key":"a","earliest_date":"2026-9-01","deadline":"2026-09-10","offices":["sha-tin"],"minimum_status":"limited"}]'},
    )
    assert status == "400 Bad Request"
    assert "YYYY-MM-DD" in body


def test_non_development_rejects_example_signing_secret(tmp_path):
    with pytest.raises(ValueError, match="example placeholder"):
        WebApplication(
            tmp_path / "web.sqlite3", signing_secret=TOKEN_SIGNING_SECRET_PLACEHOLDER,
            public_base_url="https://example.test", app_env="production",
        )
    WebApplication(
        tmp_path / "web.sqlite3", signing_secret=TOKEN_SIGNING_SECRET_PLACEHOLDER,
        public_base_url="http://127.0.0.1:8080", app_env="development",
    )
