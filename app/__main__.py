"""Explicit local entry point for the M1 quota reliability core.

Running ``python -m app`` remains side-effect free. Network polling starts only when the
operator explicitly selects the ``poll`` command.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import logging

from .config import load_settings
from .poller import QuotaPoller
from .source import GovHKQuotaSourceAdapter
from .storage import connect_database


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app")
    subcommands = parser.add_subparsers(dest="command")

    poll = subcommands.add_parser("poll", help="run the single shared GovHK quota poller")
    poll.add_argument(
        "--once",
        action="store_true",
        help="perform one observation and exit instead of running continuously",
    )
    return parser


def _print_status() -> None:
    settings = load_settings()
    print("ID Quota Alert: M1 quota reliability core.")
    print(f"Database: {settings.database_path}")
    print("GovHK Source Adapter and shared Poller are implemented but not auto-started.")
    print("Run `python -m app poll --once` for one explicit source observation.")
    print("Run `python -m app poll` only when you intend to start continuous monitoring.")
    print("Production Email notifications are not implemented yet.")


def _run_poller(*, once: bool) -> int:
    settings = load_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
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
            f"(base interval={settings.poll_interval_seconds:g}s, "
            f"jitter<= {settings.poll_jitter_seconds:g}s)."
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    if args.command != "poll":
        _print_status()
        return 0
    return _run_poller(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
