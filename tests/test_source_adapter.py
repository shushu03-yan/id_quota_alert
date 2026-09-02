from datetime import date, datetime, timedelta, timezone
import hashlib
import json

from app.observations import ObservationOutcome
from app.quota import QuotaKey, QuotaStatus
from app.source import GovHKQuotaSourceAdapter, SourceHttpResponse


OBSERVED_AT = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)


def payload_bytes(*, rows=None, offices=None, last_update="2026-08-28 07:59:00") -> bytes:
    if offices is None:
        offices = [
            {"officeId": "RHK", "eng": {"district": "Hong Kong Office"}},
            {"officeId": "FTO", "eng": {"district": "Fo Tan Office"}},
        ]
    if rows is None:
        rows = [
            {
                "date": "09/01/2026",
                "officeId": "RHK",
                "quotaR": "quota-y",
                "quotaK": "quota-r",
            },
            {
                "date": "09/01/2026",
                "officeId": "FTO",
                "quotaR": "quota-r",
                "quotaK": "quota-g",
            },
            {
                "date": "09/02/2026",
                "officeId": "RHK",
                "quotaR": "quota-r",
                "quotaK": "no-quotaK",
            },
            {
                "date": "09/02/2026",
                "officeId": "FTO",
                "quotaR": "quota-g",
                "quotaK": "quota-y",
            },
        ]
    return json.dumps(
        {
            "lastUpdateTime": last_update,
            "office": offices,
            "data": rows,
        }
    ).encode("utf-8")


def adapter_for(response_or_exception):
    captured = {}

    def transport(url, timeout_seconds, headers):
        captured["url"] = url
        captured["timeout_seconds"] = timeout_seconds
        captured["headers"] = headers
        if isinstance(response_or_exception, BaseException):
            raise response_or_exception
        return response_or_exception

    return GovHKQuotaSourceAdapter(transport=transport), captured


def test_successfully_normalizes_public_get_situation_payload() -> None:
    adapter, captured = adapter_for(SourceHttpResponse(200, payload_bytes()))

    result = adapter.read(observed_at=OBSERVED_AT)

    assert result.successful is True
    assert result.observation.outcome is ObservationOutcome.SUCCESS
    assert result.observation.office_count == 2
    assert result.observation.quota_count == 4
    assert result.observation.payload_hash == result.snapshot.payload_hash
    assert result.snapshot.source_updated_at == datetime(
        2026, 8, 27, 23, 59, tzinfo=timezone.utc
    )

    entries = result.snapshot.by_key()
    assert entries[QuotaKey(date(2026, 9, 1), "RHK")].status is QuotaStatus.LIMITED
    assert entries[QuotaKey(date(2026, 9, 1), "RHK")].service_periods == ("R",)
    assert entries[QuotaKey(date(2026, 9, 1), "FTO")].status is QuotaStatus.AVAILABLE
    assert entries[QuotaKey(date(2026, 9, 1), "FTO")].service_periods == ("K",)
    assert entries[QuotaKey(date(2026, 9, 2), "RHK")].status is QuotaStatus.UNAVAILABLE
    assert entries[QuotaKey(date(2026, 9, 2), "RHK")].service_periods == ()
    assert entries[QuotaKey(date(2026, 9, 2), "FTO")].status is QuotaStatus.AVAILABLE
    assert entries[QuotaKey(date(2026, 9, 2), "FTO")].service_periods == ("R", "K")

    assert "svcId=579" in captured["url"]
    assert captured["headers"]["Referer"].endswith("appId=579")


def test_partial_office_matrix_is_rejected_not_treated_as_disappearance() -> None:
    rows = [
        {
            "date": "09/01/2026",
            "officeId": "RHK",
            "quotaR": "quota-r",
            "quotaK": "quota-r",
        }
    ]
    adapter, _ = adapter_for(SourceHttpResponse(200, payload_bytes(rows=rows)))

    result = adapter.read(observed_at=OBSERVED_AT)

    assert result.snapshot is None
    assert result.observation.outcome is ObservationOutcome.REJECTED
    assert result.observation.error_code == "snapshot_validation"


def test_duplicate_date_office_row_is_rejected() -> None:
    duplicate = {
        "date": "09/01/2026",
        "officeId": "RHK",
        "quotaR": "quota-g",
        "quotaK": "quota-r",
    }
    rows = [duplicate, dict(duplicate)]
    offices = [{"officeId": "RHK"}]
    adapter, _ = adapter_for(
        SourceHttpResponse(200, payload_bytes(rows=rows, offices=offices))
    )

    result = adapter.read(observed_at=OBSERVED_AT)

    assert result.snapshot is None
    assert result.observation.outcome is ObservationOutcome.REJECTED
    assert result.observation.error_code == "snapshot_validation"


def test_unknown_quota_token_is_a_parse_error() -> None:
    rows = [
        {
            "date": "09/01/2026",
            "officeId": "RHK",
            "quotaR": "quota-mystery",
            "quotaK": "quota-r",
        }
    ]
    offices = [{"officeId": "RHK"}]
    adapter, _ = adapter_for(
        SourceHttpResponse(200, payload_bytes(rows=rows, offices=offices))
    )

    result = adapter.read(observed_at=OBSERVED_AT)

    assert result.snapshot is None
    assert result.observation.outcome is ObservationOutcome.PARSE_ERROR
    assert result.observation.error_code == "schema_error"


def test_invalid_json_is_a_parse_error() -> None:
    adapter, _ = adapter_for(SourceHttpResponse(200, b"{not-json"))

    result = adapter.read(observed_at=OBSERVED_AT)

    assert result.snapshot is None
    assert result.observation.outcome is ObservationOutcome.PARSE_ERROR
    assert result.observation.error_code == "invalid_json"


def test_empty_http_body_is_a_parse_error() -> None:
    adapter, _ = adapter_for(SourceHttpResponse(200, b"   \n"))

    result = adapter.read(observed_at=OBSERVED_AT)

    assert result.snapshot is None
    assert result.observation.outcome is ObservationOutcome.PARSE_ERROR
    assert result.observation.error_code == "empty_body"


def test_http_429_is_a_fetch_error() -> None:
    adapter, _ = adapter_for(SourceHttpResponse(429, b"rate limited"))

    result = adapter.read(observed_at=OBSERVED_AT)

    assert result.snapshot is None
    assert result.observation.outcome is ObservationOutcome.FETCH_ERROR
    assert result.observation.error_code == "http_429"


def test_http_5xx_is_a_fetch_error() -> None:
    adapter, _ = adapter_for(SourceHttpResponse(503, b"maintenance"))

    result = adapter.read(observed_at=OBSERVED_AT)

    assert result.snapshot is None
    assert result.observation.outcome is ObservationOutcome.FETCH_ERROR
    assert result.observation.error_code == "http_5xx"


def test_timeout_is_a_fetch_error() -> None:
    adapter, _ = adapter_for(TimeoutError("timeout"))

    result = adapter.read(observed_at=OBSERVED_AT)

    assert result.snapshot is None
    assert result.observation.outcome is ObservationOutcome.FETCH_ERROR
    assert result.observation.error_code == "timeout"


def test_source_update_time_regression_is_recorded_as_stale() -> None:
    adapter, _ = adapter_for(SourceHttpResponse(200, payload_bytes()))
    previous = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)

    result = adapter.read(
        observed_at=OBSERVED_AT + timedelta(minutes=1),
        previous_source_updated_at=previous,
    )

    assert result.snapshot is None
    assert result.observation.outcome is ObservationOutcome.STALE
    assert result.observation.error_code == "source_time_regression"
    assert result.observation.source_updated_at == datetime(
        2026, 8, 27, 23, 59, tzinfo=timezone.utc
    )
    assert result.observation.office_count == 2
    assert result.observation.quota_count == 4
    assert result.well_formed is True
    assert result.should_backoff is False


def test_same_source_time_with_different_payload_is_rejected_as_conflict() -> None:
    adapter, _ = adapter_for(SourceHttpResponse(200, payload_bytes()))

    result = adapter.read(
        observed_at=OBSERVED_AT + timedelta(minutes=1),
        previous_source_updated_at=datetime(
            2026, 8, 27, 23, 59, tzinfo=timezone.utc
        ),
        previous_payload_hash="different-accepted-payload",
    )

    assert result.snapshot is None
    assert result.observation.outcome is ObservationOutcome.REJECTED
    assert result.observation.error_code == "source_version_conflict"
    assert result.observation.source_updated_at == datetime(
        2026, 8, 27, 23, 59, tzinfo=timezone.utc
    )
    assert result.well_formed is True
    assert result.should_backoff is False


def test_same_source_time_with_same_payload_remains_a_successful_duplicate() -> None:
    body = payload_bytes()
    adapter, _ = adapter_for(SourceHttpResponse(200, body))

    result = adapter.read(
        observed_at=OBSERVED_AT + timedelta(minutes=1),
        previous_source_updated_at=datetime(
            2026, 8, 27, 23, 59, tzinfo=timezone.utc
        ),
        previous_payload_hash=hashlib.sha256(body).hexdigest(),
    )

    assert result.successful is True
    assert result.observation.outcome is ObservationOutcome.SUCCESS
    assert result.should_backoff is False


def test_missing_office_catalog_is_not_accepted() -> None:
    adapter, _ = adapter_for(
        SourceHttpResponse(200, payload_bytes(offices=[]))
    )

    result = adapter.read(observed_at=OBSERVED_AT)

    assert result.snapshot is None
    assert result.observation.outcome is ObservationOutcome.PARSE_ERROR
    assert result.observation.error_code == "schema_error"
