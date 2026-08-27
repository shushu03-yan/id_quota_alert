"""Explicit operator entry point for the quota alert service."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
import logging
import os
import socket
import time

from .admin import list_customers, list_subscriptions, outbox_status, show_subscription
from .backup import create_backup
from .config import load_settings, validate_token_signing_secret
from .email_provider import SMTPEmailProvider, SMTPSettings
from .lifecycle import (
    create_activation_code,
    create_magic_link,
    extend_zero_match_guarantees,
    list_activation_codes,
    normalize_email,
    revoke_activation_code,
)
from .notifications import EmailWorker
from .poller import QuotaPoller
from .reporting import build_health_report, build_soak_summary
from .source import GovHKQuotaSourceAdapter
from .storage import connect_database, initialize_database


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app")
    subcommands = parser.add_subparsers(dest="command")

    poll = subcommands.add_parser("poll", help="run the single shared GovHK quota poller")
    poll.add_argument("--once", action="store_true", help="perform one observation and exit")
    subcommands.add_parser("health", help="show persisted poller, source and email health")
    subcommands.add_parser("soak-summary", help="summarize the real observation window")

    worker = subcommands.add_parser("email-worker", help="run the notification outbox worker")
    worker.add_argument("--once", action="store_true", help="claim at most one email and exit")
    smoke = subcommands.add_parser("email-smoke", help="send one direct SMTP smoke-test email")
    smoke.add_argument("--to", required=True, help="test recipient email")

    activation = subcommands.add_parser("activation-code", help="manage one-time activation codes")
    activation_commands = activation.add_subparsers(dest="activation_command", required=True)
    create = activation_commands.add_parser("create")
    create.add_argument("--plan", choices=("trial", "quick", "goal", "family"), required=True)
    create.add_argument("--order-reference")
    activation_commands.add_parser("list")
    revoke = activation_commands.add_parser("revoke")
    revoke.add_argument("id", type=int)

    magic = subcommands.add_parser("magic-link", help="create a short-lived management link")
    magic.add_argument("--customer-id", type=int, required=True)
    magic.add_argument("--subscription-id", type=int)

    customer = subcommands.add_parser("customer", help="operator customer views")
    customer_sub = customer.add_subparsers(dest="customer_command", required=True)
    customer_sub.add_parser("list")

    subscription = subcommands.add_parser("subscription", help="operator subscription views")
    subscription_sub = subscription.add_subparsers(dest="subscription_command", required=True)
    subscription_sub.add_parser("list")
    subscription_show = subscription_sub.add_parser("show")
    subscription_show.add_argument("id", type=int)

    outbox = subcommands.add_parser("outbox", help="operator outbox views")
    outbox_sub = outbox.add_subparsers(dest="outbox_command", required=True)
    outbox_sub.add_parser("status")

    subcommands.add_parser("maintenance", help="run one-shot subscription maintenance")

    backup = subcommands.add_parser("backup", help="create a consistent SQLite backup")
    backup.add_argument("--directory", default="data/backups")
    backup.add_argument("--retain", type=int, default=30)

    web = subcommands.add_parser("web", help="run the activation and management web app")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8080)
    return parser


def _print_status() -> None:
    settings = load_settings()
    print("HKID Alert: launch skeleton.")
    print(f"Database: {settings.database_path}")
    print("Shared Poller, matcher/outbox, Email Worker and self-service Web are implemented.")
    print("Network polling, email delivery and Web serving start only through explicit commands.")
    print("Real provider, soak, deployment and policy validation remain operator launch gates.")


def _run_poller(*, once: bool) -> int:
    settings = load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    connection = connect_database(settings.database_path)
    source = GovHKQuotaSourceAdapter(
        service_id=settings.quota_source_service_id,
        timeout_seconds=settings.quota_source_timeout_seconds,
    )
    poller = QuotaPoller(
        connection,
        source,
        missing_confirmations_required=settings.missing_confirmations_required,
    )
    try:
        if once:
            result = poller.run_once()
            fields = [
                f"outcome={result.outcome.value}",
                f"applied={str(result.snapshot_applied).lower()}",
                f"duplicate={str(result.duplicate_snapshot).lower()}",
                f"events={result.events_created}",
            ]
            if result.error_code:
                fields.append(f"error={result.error_code}")
            print("POLL " + " ".join(fields))
            return 0 if result.successful else 1
        print(
            "Starting one shared GovHK quota poller "
            f"(base interval={settings.poll_interval_seconds:g}s, jitter<= {settings.poll_jitter_seconds:g}s)."
        )
        poller.run_forever(
            interval_seconds=settings.poll_interval_seconds,
            jitter_seconds=settings.poll_jitter_seconds,
            max_backoff_seconds=settings.poll_max_backoff_seconds,
        )
    except KeyboardInterrupt:
        print("Quota poller stopped by operator.")
        return 0
    finally:
        connection.close()
    return 0


def _database():
    connection = connect_database(load_settings().database_path)
    initialize_database(connection)
    return connection


def _smtp_provider() -> SMTPEmailProvider:
    return SMTPEmailProvider(SMTPSettings.from_environment())


def _run_email_worker(*, once: bool) -> int:
    connection = _database()
    worker = EmailWorker(connection, _smtp_provider(), worker_id=f"{socket.gethostname()}:{os.getpid()}")
    try:
        if once:
            print("EMAIL_WORKER processed=" + str(worker.run_once()).lower())
            return 0
        while True:
            if not worker.run_once():
                time.sleep(5)
    except KeyboardInterrupt:
        print("Email worker stopped by operator.")
        return 0
    finally:
        connection.close()


def _run_email_smoke(recipient: str) -> int:
    recipient = normalize_email(recipient)
    result = _smtp_provider().send(
        recipient=recipient,
        subject="HKID Alert SMTP smoke test",
        text="This is a direct provider smoke test. It is not a quota alert and does not indicate appointment availability.",
    )
    print(
        "EMAIL_SMOKE "
        f"accepted={str(result.accepted).lower()} retryable={str(result.retryable).lower()} "
        f"error={result.error_code or 'none'} provider_message_id={result.provider_message_id or 'none'}"
    )
    return 0 if result.accepted else 1


def _run_activation_code(args: argparse.Namespace) -> int:
    connection = _database()
    try:
        if args.activation_command == "create":
            code = create_activation_code(
                connection, plan_code=args.plan, now=datetime.now(timezone.utc),
                order_reference=args.order_reference,
            )
            print(code)
        elif args.activation_command == "list":
            for row in list_activation_codes(connection):
                print(
                    f"id={row['id']} plan={row['plan_code']} status={row['status']} "
                    f"expires_at={row['expires_at']} order_reference={row['order_reference'] or '-'}"
                )
        elif not revoke_activation_code(connection, args.id):
            print("Activation code was not revocable.")
            return 1
        else:
            print(f"Activation code id={args.id} revoked.")
        return 0
    finally:
        connection.close()


def _run_maintenance() -> int:
    connection = _database()
    try:
        extended = extend_zero_match_guarantees(connection, now=datetime.now(timezone.utc))
        print(f"MAINTENANCE guarantee_extensions={extended}")
        return 0
    finally:
        connection.close()


def _run_operator_view(args: argparse.Namespace) -> int:
    connection = _database()
    try:
        if args.command == "customer":
            lines = list_customers(connection)
        elif args.command == "subscription" and args.subscription_command == "list":
            lines = list_subscriptions(connection)
        elif args.command == "subscription" and args.subscription_command == "show":
            lines = show_subscription(connection, args.id)
        elif args.command == "outbox":
            lines = outbox_status(connection)
        else:
            raise AssertionError("unhandled operator view")
        print("\n".join(lines) if lines else "no rows")
        return 0
    finally:
        connection.close()


def _run_web(host: str, port: int) -> int:
    from wsgiref.simple_server import WSGIRequestHandler, make_server
    from .web import WebApplication

    secret = os.getenv("TOKEN_SIGNING_SECRET", "")
    base_url = os.getenv("PUBLIC_BASE_URL", f"http://{host}:{port}")
    settings = load_settings()
    app = WebApplication(
        settings.database_path, signing_secret=secret, public_base_url=base_url, app_env=settings.app_env
    )

    class NoTokenLoggingHandler(WSGIRequestHandler):
        def log_message(self, format, *args):
            logging.info("web request completed")

    print(f"Web app listening on {host}:{port}")
    with make_server(host, port, app, handler_class=NoTokenLoggingHandler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    if args.command is None:
        _print_status()
        return 0
    if args.command == "poll":
        return _run_poller(once=args.once)
    if args.command in {"health", "soak-summary"}:
        connection = _database()
        try:
            report = build_health_report(connection) if args.command == "health" else build_soak_summary(connection)
            print(report.render())
            return 0
        finally:
            connection.close()
    if args.command == "email-worker":
        return _run_email_worker(once=args.once)
    if args.command == "email-smoke":
        return _run_email_smoke(args.to)
    if args.command == "activation-code":
        return _run_activation_code(args)
    if args.command == "magic-link":
        settings = load_settings()
        secret = validate_token_signing_secret(os.getenv("TOKEN_SIGNING_SECRET", ""), app_env=settings.app_env)
        connection = _database()
        try:
            _, token = create_magic_link(
                connection, customer_id=args.customer_id, subscription_id=args.subscription_id,
                now=datetime.now(timezone.utc), signing_secret=secret,
            )
            print(f"{os.getenv('PUBLIC_BASE_URL', 'http://127.0.0.1:8080').rstrip('/')}/manage?token={token}")
            return 0
        finally:
            connection.close()
    if args.command in {"customer", "subscription", "outbox"}:
        return _run_operator_view(args)
    if args.command == "maintenance":
        return _run_maintenance()
    if args.command == "backup":
        path = create_backup(load_settings().database_path, backup_directory=args.directory, retain=args.retain)
        print(f"BACKUP {path}")
        return 0
    if args.command == "web":
        return _run_web(args.host, args.port)
    raise AssertionError("unhandled command")


if __name__ == "__main__":
    raise SystemExit(main())
