"""Small dependency-free WSGI product shell for activation and self-service management."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

from .config import validate_token_signing_secret
from .lifecycle import (
    PLANS,
    begin_activation,
    consume_magic_link,
    hash_secret,
    request_magic_link,
    unsubscribe_customer,
    validate_targets,
    verify_activation,
)
from .storage import _datetime_from_text, _datetime_to_text, connect_database, initialize_database


OFFICE_IDS = ("FTO", "RHK", "RKO", "RTK", "TMO", "YLO")
MAX_PUBLIC_TARGETS = 6


_BASE_CSS = """
:root{font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#172033;background:#f6f8fb}
*{box-sizing:border-box}body{margin:0}.wrap{max-width:880px;margin:0 auto;padding:28px 20px 56px}
nav{display:flex;justify-content:space-between;gap:18px;align-items:center;margin-bottom:28px}nav a{color:#324a6d;text-decoration:none;margin-left:14px}
.card{background:white;border:1px solid #e2e7ef;border-radius:16px;padding:22px;margin:16px 0;box-shadow:0 6px 24px rgba(25,45,75,.05)}
h1{font-size:34px;margin:0 0 12px}h2{font-size:21px}.muted{color:#66758a}.notice{background:#eef6ff;border-radius:12px;padding:14px 16px}
label{display:block;margin:12px 0 6px;font-weight:600}input,select{width:100%;padding:10px 12px;border:1px solid #cfd7e3;border-radius:9px;background:white}
.checks{display:flex;flex-wrap:wrap;gap:10px;margin:8px 0}.checks label{font-weight:400;margin:0;background:#f5f7fa;padding:8px 10px;border-radius:8px}.checks input{width:auto;margin-right:5px}
button,.button{display:inline-block;border:0;border-radius:10px;padding:11px 16px;background:#173f73;color:white;text-decoration:none;cursor:pointer}.secondary{background:#edf2f7;color:#173f73}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}.target{border-top:1px solid #edf0f4;padding-top:14px;margin-top:14px}.target:first-child{border-top:0;padding-top:0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:620px){.grid{grid-template-columns:1fr}h1{font-size:29px}}
footer{margin-top:34px;color:#718096;font-size:13px}footer a{color:#5b6f89;margin-right:12px}
"""


def _page(title: str, body: str) -> bytes:
    html = (
        "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)} · HKID Alert</title><style>{_BASE_CSS}</style></head><body>"
        "<div class='wrap'><nav><strong>HKID Alert</strong><div>"
        "<a href='/activate'>啟用服務</a><a href='/manage/request'>管理提醒</a></div></nav>"
        f"{body}<footer><a href='/privacy'>Privacy</a><a href='/terms'>Terms</a>"
        "<a href='/refund'>Refund</a><p>本服務並非香港政府官方服務，不會自動預約。</p></footer></div></body></html>"
    )
    return html.encode("utf-8")


def _form_multidata(environ: dict[str, object]) -> dict[str, list[str]]:
    length = int(environ.get("CONTENT_LENGTH") or 0)
    raw = environ["wsgi.input"].read(length).decode("utf-8")  # type: ignore[union-attr]
    return parse_qs(raw, keep_blank_values=True)


def _last(data: dict[str, list[str]], key: str, default: str = "") -> str:
    values = data.get(key)
    return values[-1] if values else default


def _targets_from_form(data: dict[str, list[str]]) -> list[dict[str, object]]:
    """Convert the normal form to the existing target model; keep legacy JSON POSTs testable."""
    legacy = _last(data, "targets")
    if legacy:
        loaded = json.loads(legacy)
        if not isinstance(loaded, list):
            raise ValueError("targets must be a list")
        return loaded

    targets: list[dict[str, object]] = []
    for index in range(1, MAX_PUBLIC_TARGETS + 1):
        earliest = _last(data, f"target_{index}_start")
        deadline = _last(data, f"target_{index}_end")
        offices = [value for value in data.get(f"target_{index}_office", []) if value]
        minimum = _last(data, f"target_{index}_status", "limited")
        if not earliest and not deadline and not offices:
            continue
        targets.append(
            {
                "target_key": f"target-{index}",
                "earliest_date": earliest,
                "deadline": deadline,
                "offices": offices,
                "minimum_status": minimum,
            }
        )
    return targets


def _target_form(existing: list[dict[str, object]] | None = None) -> str:
    existing = existing or []
    blocks: list[str] = []
    for index in range(1, MAX_PUBLIC_TARGETS + 1):
        target = existing[index - 1] if index <= len(existing) else {}
        earliest = escape(str(target.get("earliest_date", "")))
        deadline = escape(str(target.get("deadline", "")))
        offices = set(target.get("offices", [])) if isinstance(target.get("offices", []), list) else set()
        status = str(target.get("minimum_status", "limited"))
        office_html = "".join(
            f"<label><input type='checkbox' name='target_{index}_office' value='{office}'"
            f"{' checked' if office in offices else ''}> {office}</label>" for office in OFFICE_IDS
        )
        all_checked = " checked" if "*" in offices else ""
        blocks.append(
            f"<section class='target'><h3>預約目標 {index}{'（可選）' if index > 1 else ''}</h3>"
            "<div class='grid'>"
            f"<div><label>最早日期</label><input type='date' name='target_{index}_start' value='{earliest}'></div>"
            f"<div><label>最晚日期</label><input type='date' name='target_{index}_end' value='{deadline}'></div></div>"
            f"<label>可接受辦事處</label><div class='checks'><label><input type='checkbox' name='target_{index}_office' value='*'{all_checked}>全部</label>{office_html}</div>"
            f"<label>提醒門檻</label><select name='target_{index}_status'>"
            f"<option value='limited'{' selected' if status == 'limited' else ''}>少量名額或更好</option>"
            f"<option value='available'{' selected' if status == 'available' else ''}>僅名額充足</option></select></section>"
        )
    return "".join(blocks)


def _group_filters(rows) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for row in rows:
        key = str(row["target_key"])
        if key not in grouped:
            grouped[key] = {
                "target_key": key,
                "earliest_date": row["earliest_date"],
                "deadline": row["deadline"],
                "offices": [],
                "minimum_status": row["minimum_status"],
            }
            order.append(key)
        grouped[key]["offices"].append(str(row["office_id"]))  # type: ignore[union-attr]
    return [grouped[key] for key in order]


class WebApplication:
    def __init__(
        self, database_path: Path | str, *, signing_secret: str, public_base_url: str,
        app_env: str | None = None, clock: Callable[[], datetime] | None = None,
    ) -> None:
        validate_token_signing_secret(signing_secret, app_env=app_env or os.getenv("APP_ENV", "development"))
        self.database_path = Path(database_path)
        self.signing_secret = signing_secret
        self.public_base_url = public_base_url.rstrip("/")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, environ, start_response):
        path, method = environ.get("PATH_INFO", "/"), environ.get("REQUEST_METHOD", "GET")
        connection = connect_database(self.database_path)
        initialize_database(connection)
        try:
            if path == "/" and method == "GET":
                body = _page("公開預約名額變化提醒", """
                <div class='card'><p class='muted'>香港身份證公開預約名額提醒</p>
                <h1>少刷新網頁，把注意力留給真正需要處理的提醒。</h1>
                <p>系統使用同一個共享監測流程讀取公開名額資訊。當公開名額變化符合你的日期、辦事處和提醒門檻時，以 Email 通知你。</p>
                <div class='notice'>只做提醒，不會代約、不會自動填表，也不會繞過 CAPTCHA 或官方排隊機制。</div>
                <div class='actions'><a class='button' href='/activate'>我已有激活碼</a><a class='button secondary' href='/manage/request'>管理我的提醒</a></div></div>""")
                return self._respond(start_response, "200 OK", body)

            if path == "/activate" and method == "GET":
                body = _page("啟用 HKID Alert", "<div class='card'><h1>啟用提醒</h1>"
                    "<p class='muted'>輸入激活碼和 Email，再設定至少一個預約目標。公開版本最多顯示 6 個目標欄位；實際上限仍由套餐規則控制。</p>"
                    "<form method='post'><label>激活碼</label><input name='code' autocomplete='off' required>"
                    "<label>Email</label><input name='email' type='email' autocomplete='email' required>"
                    + _target_form() + "<div class='actions'><button>發送驗證郵件</button></div></form></div>")
                return self._respond(start_response, "200 OK", body)

            if path == "/activate" and method == "POST":
                data = _form_multidata(environ)
                begin_activation(
                    connection, code=_last(data, "code"), email=_last(data, "email"),
                    targets=_targets_from_form(data), now=self.clock(),
                    signing_secret=self.signing_secret,
                )
                return self._respond(start_response, "202 Accepted", _page("請驗證 Email", "<div class='card'><h1>檢查你的 Email</h1><p>驗證郵件已進入發送隊列。只有完成 Email 驗證後，套餐計時才會開始。</p></div>"))

            if path == "/verify" and method == "GET":
                token = parse_qs(environ.get("QUERY_STRING", "")).get("token", [""])[-1]
                verify_activation(connection, token=token, now=self.clock())
                return self._respond(start_response, "200 OK", _page("服務已啟用", "<div class='card'><h1>服務已啟用</h1><p>啟用確認郵件已進入發送隊列。這封確認郵件不代表目前有預約名額。</p></div>"))

            if path == "/manage/request" and method == "GET":
                return self._respond(start_response, "200 OK", _page("管理提醒", """
                    <div class='card'><h1>管理我的提醒</h1><p class='muted'>輸入啟用服務時使用的 Email，我們會寄出一條短時效管理連結。</p>
                    <form method='post'><label>Email</label><input name='email' type='email' required><div class='actions'><button>寄出管理連結</button></div></form></div>"""))

            if path == "/manage/request" and method == "POST":
                data = _form_multidata(environ)
                # Keep the response identical whether an active subscription exists to avoid account enumeration.
                try:
                    request_magic_link(
                        connection, email=_last(data, "email"), now=self.clock(),
                        signing_secret=self.signing_secret,
                    )
                except ValueError:
                    pass
                return self._respond(start_response, "202 Accepted", _page("檢查你的 Email", "<div class='card'><h1>請檢查 Email</h1><p>如果該 Email 有有效服務，管理連結會進入發送隊列。</p></div>"))

            if path == "/manage" and method == "GET":
                token = parse_qs(environ.get("QUERY_STRING", "")).get("token", [""])[-1]
                now = self.clock()
                row = connection.execute(
                    "SELECT * FROM magic_link_tokens WHERE token_hash=? AND used_at IS NULL", (hash_secret(token),)
                ).fetchone()
                if row is None or (_datetime_from_text(row["expires_at"]) or now) <= now:
                    raise ValueError("management link unavailable")
                subscription = connection.execute(
                    """SELECT id, customer_id, plan_code, starts_at, expires_at
                       FROM subscriptions WHERE id=? AND active=1
                       AND starts_at <= ? AND expires_at > ?""",
                    (row["subscription_id"], _datetime_to_text(now), _datetime_to_text(now)),
                ).fetchone()
                if subscription is None:
                    raise ValueError("active subscription not found")
                filters = connection.execute(
                    "SELECT target_key, earliest_date, deadline, office_id, minimum_status FROM subscription_filters WHERE subscription_id=? ORDER BY target_key,id",
                    (subscription["id"],),
                ).fetchall()
                targets = _group_filters(filters)
                summary = (
                    f"<p><strong>套餐：</strong>{escape(str(subscription['plan_code']).title())}</p>"
                    f"<p><strong>有效期：</strong>{escape(str(subscription['starts_at'])[:10])} ～ {escape(str(subscription['expires_at'])[:10])}</p>"
                )
                body = _page("管理預約目標", "<div class='card'><h1>管理預約目標</h1>" + summary
                    + f"<form method='post'><input type='hidden' name='token' value='{escape(token)}'>"
                    + _target_form(targets) + "<div class='actions'><button>保存新目標</button></div></form>"
                    + f"<form method='post' action='/unsubscribe'><input type='hidden' name='token' value='{escape(token)}'><div class='actions'><button class='secondary'>停止服務並退訂</button></div></form></div>")
                return self._respond(start_response, "200 OK", body)

            if path == "/manage" and method == "POST":
                data, now = _form_multidata(environ), self.clock()
                customer_id, subscription_id = consume_magic_link(connection, token=_last(data, "token"), now=now)
                if subscription_id is None:
                    raise ValueError("link has no subscription")
                subscription = connection.execute(
                    """SELECT plan_code FROM subscriptions
                       WHERE id=? AND customer_id=? AND active=1
                       AND starts_at <= ? AND expires_at > ?""",
                    (subscription_id, customer_id, _datetime_to_text(now), _datetime_to_text(now)),
                ).fetchone()
                if subscription is None:
                    raise ValueError("active subscription not found")
                targets = validate_targets(
                    str(subscription["plan_code"]), _targets_from_form(data),
                    business_date=now.astimezone(timezone(timedelta(hours=8))).date(),
                )
                connection.execute("DELETE FROM subscription_filters WHERE subscription_id=?", (subscription_id,))
                for target in targets:
                    for office in target["offices"]:
                        connection.execute(
                            "INSERT INTO subscription_filters(subscription_id,target_key,earliest_date,deadline,office_id,minimum_status) VALUES (?,?,?,?,?,?)",
                            (subscription_id, target["target_key"], target["earliest_date"], target["deadline"], office, target["minimum_status"]),
                        )
                connection.commit()
                return self._respond(start_response, "200 OK", _page("目標已更新", "<div class='card'><h1>已保存</h1><p>新的預約目標已生效。下次修改時請重新申請管理連結。</p></div>"))

            if path == "/unsubscribe" and method == "GET":
                token = parse_qs(environ.get("QUERY_STRING", "")).get("token", [""])[-1]
                body = _page("確認退訂", f"<div class='card'><h1>確認停止所有提醒？</h1><p>此操作會停用目前服務並取消尚未發出的配額提醒。</p><form method='post'><input type='hidden' name='token' value='{escape(token)}'><button>確認退訂</button></form></div>")
                return self._respond(start_response, "200 OK", body)

            if path == "/unsubscribe" and method == "POST":
                data, now = _form_multidata(environ), self.clock()
                customer_id, _ = consume_magic_link(connection, token=_last(data, "token"), now=now)
                unsubscribe_customer(connection, customer_id=customer_id, now=now)
                return self._respond(start_response, "200 OK", _page("已退訂", "<div class='card'><h1>已停止服務</h1><p>訂閱和未發送的配額提醒已停止。</p></div>"))

            if path == "/privacy" and method == "GET":
                return self._respond(start_response, "200 OK", _page("Privacy", "<div class='card'><h1>Privacy Notice（試運行草案）</h1><p>服務只需要 Email、預約日期偏好、辦事處偏好、提醒門檻及必要的訂閱/投遞記錄。</p><p>不需要亦不應提交 HKID、護照/旅行證件號碼、出生日期、簽證號碼、政府預約密碼或驗證碼。</p><p class='muted'>正式公開收費前應由營運者確認最終版本、資料保留期限及聯絡方式。</p></div>"))

            if path == "/terms" and method == "GET":
                return self._respond(start_response, "200 OK", _page("Terms", "<div class='card'><h1>Terms（試運行草案）</h1><p>本服務提供公開預約名額變化提醒，不提供自動預約、代約或成功預約保證。收到提醒後，使用者仍需自行進入官方系統完成預約。</p><p class='muted'>正式公開收費前需補齊最終服務條款及適用法律資訊。</p></div>"))

            if path == "/refund" and method == "GET":
                return self._respond(start_response, "200 OK", _page("Refund", "<div class='card'><h1>Refund Policy（試運行草案）</h1><p>退款與售後仍由試運行期間的人工訂單流程處理。Goal / Family 的保障僅指符合條件時一次性延長服務，不等於預約成功保障。</p><p class='muted'>正式收費前需確定最終退款條件、處理時限與聯絡方式。</p></div>"))

            return self._respond(start_response, "404 Not Found", _page("未找到", "<div class='card'><h1>404</h1><p>找不到這個頁面。</p></div>"))
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._respond(start_response, "400 Bad Request", _page("無法處理", f"<div class='card'><h1>無法處理</h1><p>{escape(str(exc))}</p></div>"))
        finally:
            connection.close()

    @staticmethod
    def _respond(start_response, status: str, body: bytes):
        start_response(
            status,
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Content-Type-Options", "nosniff"),
                ("X-Frame-Options", "DENY"),
                ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
                (
                    "Content-Security-Policy",
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
                ),
            ],
        )
        return [body]
