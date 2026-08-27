from datetime import datetime, timedelta, timezone
import sqlite3

from app.backup import create_backup
from app.storage import connect_database, initialize_database


NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)


def test_backup_uses_consistent_sqlite_image_and_rotates(tmp_path):
    database = tmp_path / "live.sqlite3"
    connection = connect_database(database)
    initialize_database(connection)
    connection.execute("INSERT INTO customers(email_normalized,created_at,consent_source) VALUES ('u@example.com','2026-08-28T00:00:00Z','test')")
    connection.commit()
    directory = tmp_path / "backups"
    first = create_backup(database, backup_directory=directory, retain=2, now=NOW)
    create_backup(database, backup_directory=directory, retain=2, now=NOW + timedelta(seconds=1))
    third = create_backup(database, backup_directory=directory, retain=2, now=NOW + timedelta(seconds=2))
    assert len(list(directory.glob("*.sqlite3"))) == 2
    assert not first.exists() and third.exists()
    with sqlite3.connect(third) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT email_normalized FROM customers").fetchone()[0] == "u@example.com"
