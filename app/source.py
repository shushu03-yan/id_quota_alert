"""GovHK / Immigration Department public quota source adapter.

The source layer is deliberately fail-closed. HTTP failures, malformed JSON, unknown
quota status tokens and incomplete office/date coverage produce failed observations.
A complete but older source snapshot is recorded as stale and never returned as a
``ValidatedSnapshot`` that could mutate confirmed quota state.

The public quota preview currently uses service id 579 and the ``getSituation`` JSON
endpoint. All users must share one poller calling this adapter; this module is not a
per-user fetch client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import json
import socket
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from app.observations import ObservationOutcome, QuotaObservation
from app.quota import (
    QuotaEntry,
    QuotaKey,
    QuotaStatus,
    SnapshotValidationError,
    ValidatedSnapshot,
    validate_snapshot,
)


GOVHK_QUOTA_API_URL = (
    "https://eservices.es2.immd.gov.hk/surgecontrolgate/ticket/getSituation"
)
GOVHK_QUOTA_PREVIEW_URL = (
    "https://eservices.es2.immd.gov.hk/es/quota-enquiry-client/?l=zh-CN&appId=579"
)
DEFAULT_SERVICE_ID = 579
PARSER_VERSION = "govhk-get-situation-v1"
HONG_KONG_TZ = ZoneInfo("Asia/Hong_Kong")


class SourceParseError(ValueError):
    """The source response could not be safely interpreted."""

    def __init__(self, message: str, *, code: str = "schema_error") -> None:
        super().__init__(message)
        self.code = code


class SourceRejectedError(ValueError):
    """The source response parsed, but is unsafe to apply as a snapshot."""

    def __init__(self, message: str, *, code: str = "snapshot_rejected") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SourceHttpResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceReadResult:
    observation: QuotaObservation
    snapshot: ValidatedSnapshot | None

    @property
    def successful(self) -> bool:
        return self.snapshot is not None and self.observation.may_update_state

    @property
    def well_formed(self) -> bool:
        """Whether the endpoint returned a complete, parseable quota matrix."""

        return self.observation.outcome in {
            ObservationOutcome.SUCCESS,
            ObservationOutcome.STALE,
        } or self.observation.error_code == "source_version_conflict"

    @property
    def should_backoff(self) -> bool:
        """Back off only for transport, parsing, or unsafe structural failures."""

        if self.observation.outcome is ObservationOutcome.STALE:
            return False
        if self.observation.error_code == "source_version_conflict":
            return False
        return not self.successful


Transport = Callable[[str, float, Mapping[str, str]], SourceHttpResponse]


def _default_transport(
    url: str,
    timeout_seconds: float,
    headers: Mapping[str, str],
) -> SourceHttpResponse:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            return SourceHttpResponse(
                status_code=int(response.getcode()),
                body=response.read(),
                headers=dict(response.headers.items()),
            )
    except HTTPError as exc:
        # HTTP errors are returned as normal responses so the adapter can classify
        # 403 / 429 / 5xx without leaking body details into logs or exceptions.
        try:
            body = exc.read()
        except Exception:
            body = b""
        return SourceHttpResponse(status_code=int(exc.code), body=body)
    except socket.timeout as exc:
        raise TimeoutError("quota source request timed out") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, socket.timeout):
            raise TimeoutError("quota source request timed out") from exc
        raise ConnectionError("quota source connection failed") from exc


def _parse_quota_date(value: object) -> date:
    if not isinstance(value, str) or not value.strip():
        raise SourceParseError("quota row date must be a non-empty string")

    text = value.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise SourceParseError(f"unsupported quota date format: {text!r}")


def _parse_source_updated_at(value: object) -> datetime | None:
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise SourceParseError("invalid source update timestamp") from exc

    if not isinstance(value, str):
        raise SourceParseError("lastUpdateTime must be a string, number, or null")

    text = value.strip()
    if not text:
        return None

    if text.isdigit():
        return _parse_source_updated_at(int(text))

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None

    if parsed is None:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%m/%d/%Y %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                pass

    if parsed is None:
        raise SourceParseError(f"unsupported lastUpdateTime format: {text!r}")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=HONG_KONG_TZ)
    return parsed.astimezone(timezone.utc)


def _parse_period_status(value: object, *, field_name: str) -> QuotaStatus:
    if not isinstance(value, str) or not value.strip():
        raise SourceParseError(f"{field_name} must be a non-empty string")

    token = value.strip().lower()
    if token == "quota-g":
        return QuotaStatus.AVAILABLE
    if token == "quota-y":
        return QuotaStatus.LIMITED
    if token in {"quota-r", "quota-non"} or token.startswith("no-quota"):
        return QuotaStatus.UNAVAILABLE
    raise SourceParseError(f"unknown {field_name} status token: {value!r}")


def _aggregate_status(period_statuses: Mapping[str, QuotaStatus]) -> QuotaStatus:
    return max(period_statuses.values(), key=lambda status: status.rank)


class GovHKQuotaParser:
    """Parse the public ``getSituation`` JSON into the normalized quota model."""

    parser_version = PARSER_VERSION

    def parse(
        self,
        body: bytes,
        *,
        observed_at: datetime,
        payload_hash: str,
    ) -> ValidatedSnapshot:
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SourceParseError("quota payload is not valid UTF-8", code="invalid_encoding") from exc

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceParseError("quota payload is not valid JSON", code="invalid_json") from exc

        if not isinstance(payload, dict):
            raise SourceParseError("quota payload root must be an object")

        rows = payload.get("data")
        offices = payload.get("office")
        if not isinstance(rows, list) or not rows:
            raise SourceParseError("quota payload must contain non-empty data[]")
        if not isinstance(offices, list) or not offices:
            raise SourceParseError("quota payload must contain non-empty office[]")

        office_ids: set[str] = set()
        for office in offices:
            if not isinstance(office, dict):
                raise SourceParseError("every office entry must be an object")
            office_id = office.get("officeId")
            if not isinstance(office_id, str) or not office_id.strip():
                raise SourceParseError("every office entry must have officeId")
            office_id = office_id.strip().upper()
            if office_id in office_ids:
                raise SourceParseError(f"duplicate officeId in office[]: {office_id}")
            office_ids.add(office_id)

        source_updated_at = _parse_source_updated_at(payload.get("lastUpdateTime"))
        entries: list[QuotaEntry] = []
        dates: set[date] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise SourceParseError("every data entry must be an object")

            office_id_value = row.get("officeId")
            if not isinstance(office_id_value, str) or not office_id_value.strip():
                raise SourceParseError("every data entry must have officeId")
            office_id = office_id_value.strip().upper()
            if office_id not in office_ids:
                raise SourceParseError(
                    f"data row references officeId missing from office[]: {office_id}"
                )

            quota_date = _parse_quota_date(row.get("date"))
            dates.add(quota_date)

            if "quotaR" not in row or "quotaK" not in row:
                raise SourceParseError("every data entry must include quotaR and quotaK")

            period_statuses = {
                "R": _parse_period_status(row["quotaR"], field_name="quotaR"),
                "K": _parse_period_status(row["quotaK"], field_name="quotaK"),
            }
            active_periods = tuple(
                period
                for period in ("R", "K")
                if period_statuses[period] is not QuotaStatus.UNAVAILABLE
            )
            entries.append(
                QuotaEntry(
                    key=QuotaKey(quota_date, office_id),
                    status=_aggregate_status(period_statuses),
                    service_periods=active_periods,
                )
            )

        # The public endpoint is a date × office matrix. Enforcing that matrix is the
        # main protection against a partial response being mistaken for quota loss.
        expected_keys = {
            QuotaKey(quota_date, office_id)
            for quota_date in dates
            for office_id in office_ids
        }

        try:
            return validate_snapshot(
                sorted(entries, key=lambda entry: entry.key),
                observed_at=observed_at,
                source_updated_at=source_updated_at,
                payload_hash=payload_hash,
                parser_version=self.parser_version,
                expected_keys=expected_keys,
                minimum_entries=len(expected_keys),
            )
        except SnapshotValidationError as exc:
            raise SourceRejectedError(str(exc), code="snapshot_validation") from exc


class GovHKQuotaSourceAdapter:
    """Fetch and safely normalize one observation from the public quota endpoint."""

    def __init__(
        self,
        *,
        api_url: str = GOVHK_QUOTA_API_URL,
        preview_url: str = GOVHK_QUOTA_PREVIEW_URL,
        service_id: int = DEFAULT_SERVICE_ID,
        timeout_seconds: float = 20.0,
        transport: Transport = _default_transport,
        parser: GovHKQuotaParser | None = None,
    ) -> None:
        if service_id <= 0:
            raise ValueError("service_id must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.api_url = api_url
        self.preview_url = preview_url
        self.service_id = service_id
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.parser = parser or GovHKQuotaParser()

    def _request_url(self, observed_at: datetime) -> str:
        cache_buster_ms = int(observed_at.timestamp() * 1000)
        separator = "&" if "?" in self.api_url else "?"
        return (
            f"{self.api_url}{separator}"
            + urlencode({"svcId": str(self.service_id), "t": str(cache_buster_ms)})
        )

    def read(
        self,
        *,
        observed_at: datetime | None = None,
        previous_source_updated_at: datetime | None = None,
        previous_payload_hash: str | None = None,
    ) -> SourceReadResult:
        observed_at = observed_at or datetime.now(timezone.utc)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if previous_source_updated_at is not None and (
            previous_source_updated_at.tzinfo is None
            or previous_source_updated_at.utcoffset() is None
        ):
            raise ValueError("previous_source_updated_at must be timezone-aware")

        headers = {
            "Accept": "application/json,text/plain,*/*",
            "Referer": self.preview_url,
            "User-Agent": "HKIDQuotaAlert/0.0.1 (+public-quota-monitor)",
        }

        try:
            response = self.transport(
                self._request_url(observed_at),
                self.timeout_seconds,
                headers,
            )
        except TimeoutError:
            return self._failure(observed_at, ObservationOutcome.FETCH_ERROR, "timeout")
        except ConnectionError:
            return self._failure(
                observed_at,
                ObservationOutcome.FETCH_ERROR,
                "connection_error",
            )
        except OSError:
            return self._failure(
                observed_at,
                ObservationOutcome.FETCH_ERROR,
                "transport_error",
            )

        if not 200 <= response.status_code < 300:
            if response.status_code == 403:
                code = "http_403"
            elif response.status_code == 429:
                code = "http_429"
            elif 500 <= response.status_code < 600:
                code = "http_5xx"
            else:
                code = f"http_{response.status_code}"
            return self._failure(observed_at, ObservationOutcome.FETCH_ERROR, code)

        body = response.body
        if not body or not body.strip():
            return self._failure(
                observed_at,
                ObservationOutcome.PARSE_ERROR,
                "empty_body",
            )

        payload_hash = hashlib.sha256(body).hexdigest()
        try:
            snapshot = self.parser.parse(
                body,
                observed_at=observed_at,
                payload_hash=payload_hash,
            )
        except SourceParseError as exc:
            return self._failure(
                observed_at,
                ObservationOutcome.PARSE_ERROR,
                exc.code,
                payload_hash=payload_hash,
                parser_version=self.parser.parser_version,
            )
        except SourceRejectedError as exc:
            return self._failure(
                observed_at,
                ObservationOutcome.REJECTED,
                exc.code,
                payload_hash=payload_hash,
                parser_version=self.parser.parser_version,
            )

        office_count = len({entry.key.office_id for entry in snapshot.entries})
        if (
            previous_source_updated_at is not None
            and snapshot.source_updated_at is not None
        ):
            if snapshot.source_updated_at < previous_source_updated_at:
                return self._validated_rejection(
                    snapshot,
                    outcome=ObservationOutcome.STALE,
                    error_code="source_time_regression",
                    office_count=office_count,
                )
            if (
                snapshot.source_updated_at == previous_source_updated_at
                and previous_payload_hash is not None
                and snapshot.payload_hash != previous_payload_hash
            ):
                return self._validated_rejection(
                    snapshot,
                    outcome=ObservationOutcome.REJECTED,
                    error_code="source_version_conflict",
                    office_count=office_count,
                )

        observation = QuotaObservation(
            observed_at=observed_at,
            outcome=ObservationOutcome.SUCCESS,
            source_updated_at=snapshot.source_updated_at,
            payload_hash=payload_hash,
            parser_version=snapshot.parser_version,
            office_count=office_count,
            quota_count=len(snapshot.entries),
        )
        return SourceReadResult(observation=observation, snapshot=snapshot)

    @staticmethod
    def _validated_rejection(
        snapshot: ValidatedSnapshot,
        *,
        outcome: ObservationOutcome,
        error_code: str,
        office_count: int,
    ) -> SourceReadResult:
        """Keep audit metadata for a complete snapshot that is unsafe to apply."""

        return SourceReadResult(
            observation=QuotaObservation(
                observed_at=snapshot.observed_at,
                outcome=outcome,
                source_updated_at=snapshot.source_updated_at,
                payload_hash=snapshot.payload_hash,
                parser_version=snapshot.parser_version,
                office_count=office_count,
                quota_count=len(snapshot.entries),
                error_code=error_code,
            ),
            snapshot=None,
        )

    @staticmethod
    def _failure(
        observed_at: datetime,
        outcome: ObservationOutcome,
        error_code: str,
        *,
        payload_hash: str | None = None,
        parser_version: str | None = None,
    ) -> SourceReadResult:
        return SourceReadResult(
            observation=QuotaObservation(
                observed_at=observed_at,
                outcome=outcome,
                payload_hash=payload_hash,
                parser_version=parser_version,
                error_code=error_code,
            ),
            snapshot=None,
        )
