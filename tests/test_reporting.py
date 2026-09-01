from datetime import datetime, timedelta, timezone

from app.reporting import build_health_report, build_soak_summary
from app.storage import connect_database, initialize_database, set_runtime_state


NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)


def _db(tmp_path):
    connection = connect_database(tmp_path / "report.sqlite3")
    initialize_database(connection)
    return connection


def _observation(connection, outcome, observed_at, error=None):
    connection.execute(
        "INSERT INTO quota_observations(observed_at,outcome,error_code) VALUES (?,?,?)",
        (observed_at.isoformat().replace("+00:00", "Z"), outcome, error),
    )
    connection.commit()


def test_health_and_soak_report_have_explicit_empty_state(tmp_path):
    connection = _db(tmp_path)
    health = build_health_report(connection, now=NOW)
    soak = build_soak_summary(connection)
    assert health.values["observation_count"] == 0
    assert health.poller_status == "NOT RUN"
    assert "SOAK TEST NOT COMPLETE" in soak.render()


def test_all_success_summary_uses_exact_percentages(tmp_path):
    connection = _db(tmp_path)
    _observation(connection, "success", NOW - timedelta(hours=1))
    _observation(connection, "success", NOW)
    summary = build_soak_summary(connection)
    assert summary.values["success_percent"] == "100.00%"
    assert summary.values["fetch_error_percent"] == "0.00%"


def test_fetch_and_parse_failures_are_reported_separately(tmp_path):
    connection = _db(tmp_path)
    _observation(connection, "fetch_error", NOW - timedelta(minutes=2), "http_429")
    _observation(connection, "parse_error", NOW, "invalid_json")
    summary = build_soak_summary(connection)
    assert summary.values["fetch_error_percent"] == "50.00%"
    assert summary.values["parse_error_percent"] == "50.00%"
    assert summary.values["429_count"] == 1


def test_generic_5xx_and_source_regressions_are_counted(tmp_path):
    connection = _db(tmp_path)
    _observation(connection, "fetch_error", NOW - timedelta(minutes=2), "http_5xx")
    _observation(connection, "rejected", NOW, "source_time_regression")
    summary = build_soak_summary(connection)
    assert summary.values["5xx_count"] == 1
    assert summary.values["source_time_regression_count"] == 1


def test_health_marks_old_success_as_stale(tmp_path):
    connection = _db(tmp_path)
    old = NOW - timedelta(minutes=10)
    set_runtime_state(connection, "last_successful_poll", old.isoformat().replace("+00:00", "Z"), updated_at=old)
    set_runtime_state(connection, "last_valid_snapshot", old.isoformat().replace("+00:00", "Z"), updated_at=old)
    connection.commit()
    report = build_health_report(connection, now=NOW, stale_after=timedelta(minutes=5))
    assert report.poller_status == "STALE"
    assert report.source_status == "STALE"


def test_runtime_under_three_days_never_completes_soak(tmp_path):
    connection = _db(tmp_path)
    _observation(connection, "success", NOW - timedelta(days=2, hours=23))
    _observation(connection, "success", NOW)
    assert build_soak_summary(connection).complete is False


def test_three_day_window_requires_manual_review_not_passed_label(tmp_path):
    connection = _db(tmp_path)
    _observation(connection, "success", NOW - timedelta(days=3))
    _observation(connection, "success", NOW)
    rendered = build_soak_summary(connection).render()
    assert "MANUAL REVIEW REQUIRED" in rendered
    assert "PASSED" not in rendered
