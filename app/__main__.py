"""Small project status entry point.

The network poller and notification worker are intentionally not started yet. Running
`python -m app` should never imply that production monitoring is active during M1.
"""

from .config import load_settings


def main() -> None:
    settings = load_settings()
    print("ID Quota Alert: M1 reliability core is under development.")
    print(f"Database: {settings.database_path}")
    print(
        "GovHK polling and production notifications are not implemented or running yet."
    )


if __name__ == "__main__":
    main()
