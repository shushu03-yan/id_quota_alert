from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urlencode

from app.lifecycle import begin_activation, create_activation_code, create_magic_link, verification_token
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
    app = WebApplication(path, signing_secret=SECRET, public_base_url="https://example.test")
    assert _request(app, "/activate")[0] == "200 OK"
    assert _request(app, "/verify", query=urlencode({"token": token}))[0] == "200 OK"
    sub = connection.execute("SELECT id,customer_id FROM subscriptions").fetchone()
    _, magic = create_magic_link(connection, customer_id=sub["customer_id"], subscription_id=sub["id"], now=NOW, signing_secret=SECRET)
    assert _request(app, "/manage", query=urlencode({"token": magic}))[0] == "200 OK"
    assert _request(app, "/unsubscribe", query=urlencode({"token": magic}))[0] == "200 OK"
    assert _request(app, "/unsubscribe", method="POST", form={"token": magic})[0] == "200 OK"
    assert connection.execute("SELECT active FROM subscriptions").fetchone()[0] == 0
