"""Tiny dependency-free WSGI MVP for activation, verification and management."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
from urllib.parse import parse_qs

from .config import validate_token_signing_secret
from .lifecycle import (
    PLANS, begin_activation, consume_magic_link, hash_secret, normalize_email,
    unsubscribe_customer, validate_targets, verify_activation,
)
from .storage import _datetime_from_text, connect_database, initialize_database


def _page(title: str, body: str) -> bytes:
    return ("<!doctype html><html lang='zh-Hant'><meta charset='utf-8'>"
            f"<title>{escape(title)}</title><body><main><h1>{escape(title)}</h1>{body}</main></body></html>").encode()


def _form_data(environ: dict[str, object]) -> dict[str, str]:
    length = int(environ.get("CONTENT_LENGTH") or 0)
    raw = environ["wsgi.input"].read(length).decode("utf-8")  # type: ignore[union-attr]
    return {key: values[-1] for key, values in parse_qs(raw).items()}


class WebApplication:
    def __init__(
        self, database_path: Path | str, *, signing_secret: str, public_base_url: str,
        app_env: str | None = None,
    ) -> None:
        validate_token_signing_secret(signing_secret, app_env=app_env or os.getenv("APP_ENV", "development"))
        self.database_path = Path(database_path)
        self.signing_secret = signing_secret
        self.public_base_url = public_base_url.rstrip("/")

    def __call__(self, environ, start_response):
        path, method = environ.get("PATH_INFO", "/"), environ.get("REQUEST_METHOD", "GET")
        connection = connect_database(self.database_path)
        initialize_database(connection)
        try:
            if path == "/activate" and method == "GET":
                body = _page("启动 HKID Alert", """
                <p>仅提供公开预约配额变化提醒，不会自动预约。</p>
                <form method='post'><label>激活码 <input name='code' required></label><br>
                <label>Email <input name='email' type='email' required></label><br>
                <label>预约目标 JSON <textarea name='targets' required></textarea></label><br>
                <button>发送验证邮件</button></form>""")
                return self._respond(start_response, "200 OK", body)
            if path == "/activate" and method == "POST":
                data = _form_data(environ)
                begin_activation(connection, code=data["code"], email=data["email"],
                    targets=json.loads(data["targets"]), now=datetime.now(timezone.utc),
                    base_url=self.public_base_url, signing_secret=self.signing_secret)
                return self._respond(start_response, "202 Accepted", _page("请验证邮箱", "<p>验证邮件已进入发送队列。</p>"))
            if path == "/verify" and method == "GET":
                token = parse_qs(environ.get("QUERY_STRING", "")).get("token", [""])[-1]
                verify_activation(connection, token=token, now=datetime.now(timezone.utc))
                return self._respond(start_response, "200 OK", _page("服务已启动", "<p>启动确认邮件已进入发送队列。</p>"))
            if path == "/manage" and method == "GET":
                token = parse_qs(environ.get("QUERY_STRING", "")).get("token", [""])[-1]
                row = connection.execute(
                    "SELECT * FROM magic_link_tokens WHERE token_hash=? AND used_at IS NULL", (hash_secret(token),)
                ).fetchone()
                if row is None or (_datetime_from_text(row["expires_at"]) or datetime.now(timezone.utc)) <= datetime.now(timezone.utc):
                    raise ValueError("management link unavailable")
                filters = connection.execute("SELECT target_key, earliest_date, deadline, office_id, minimum_status FROM subscription_filters WHERE subscription_id=?", (row["subscription_id"],)).fetchall()
                body = "<pre>" + escape(json.dumps([dict(x) for x in filters], ensure_ascii=False, indent=2)) + "</pre>"
                body += f"<form method='post'><input type='hidden' name='token' value='{escape(token)}'><textarea name='targets' required></textarea><button>更新目标</button></form>"
                body += f"<form method='post' action='/unsubscribe'><input type='hidden' name='token' value='{escape(token)}'><button>停止并退订</button></form>"
                return self._respond(start_response, "200 OK", _page("管理预约目标", body))
            if path == "/manage" and method == "POST":
                data, now = _form_data(environ), datetime.now(timezone.utc)
                customer_id, subscription_id = consume_magic_link(connection, token=data["token"], now=now)
                if subscription_id is None:
                    raise ValueError("link has no subscription")
                plan_code = connection.execute("SELECT plan_code FROM subscriptions WHERE id=? AND customer_id=?", (subscription_id, customer_id)).fetchone()[0]
                targets = validate_targets(
                    plan_code, json.loads(data["targets"]),
                    business_date=now.astimezone(timezone(timedelta(hours=8))).date(),
                )
                connection.execute("DELETE FROM subscription_filters WHERE subscription_id=?", (subscription_id,))
                for target in targets:
                    for office in target["offices"]:
                        connection.execute("INSERT INTO subscription_filters(subscription_id,target_key,earliest_date,deadline,office_id,minimum_status) VALUES (?,?,?,?,?,?)",
                            (subscription_id, target["target_key"], target["earliest_date"], target["deadline"], office, target["minimum_status"]))
                connection.commit()
                return self._respond(start_response, "200 OK", _page("目标已更新", "<p>新的目标已保存。</p>"))
            if path == "/unsubscribe" and method == "GET":
                token = parse_qs(environ.get("QUERY_STRING", "")).get("token", [""])[-1]
                body = f"<p>确认停止所有提醒并退订。</p><form method='post'><input type='hidden' name='token' value='{escape(token)}'><button>确认退订</button></form>"
                return self._respond(start_response, "200 OK", _page("确认退订", body))
            if path == "/unsubscribe" and method == "POST":
                data, now = _form_data(environ), datetime.now(timezone.utc)
                customer_id, _ = consume_magic_link(connection, token=data["token"], now=now)
                unsubscribe_customer(connection, customer_id=customer_id, now=now)
                return self._respond(start_response, "200 OK", _page("已退订", "<p>订阅和未发送的配额提醒已停止。</p>"))
            return self._respond(start_response, "404 Not Found", _page("未找到", ""))
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._respond(start_response, "400 Bad Request", _page("无法处理", f"<p>{escape(str(exc))}</p>"))
        finally:
            connection.close()

    @staticmethod
    def _respond(start_response, status: str, body: bytes):
        start_response(status, [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))),
                                ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer")])
        return [body]
