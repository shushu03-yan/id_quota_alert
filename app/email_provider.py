"""Provider-neutral email contract and Tencent Cloud SES API implementation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
import re
from typing import Protocol


TEMPLATE_KINDS = (
    "verify_email",
    "activation_test",
    "quota_alert",
    "manage_link",
)


@dataclass(frozen=True, slots=True)
class EmailDeliveryRequest:
    recipient: str
    subject: str
    template_kind: str
    template_data: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.template_kind not in TEMPLATE_KINDS:
            raise ValueError(f"unsupported template kind: {self.template_kind}")
        if not self.recipient.strip() or not self.subject.strip():
            raise ValueError("recipient and subject are required")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in self.template_data.items()):
            raise ValueError("template data must contain string keys and values")


@dataclass(frozen=True, slots=True)
class EmailDeliveryResult:
    accepted: bool
    provider_message_id: str | None = None
    retryable: bool = False
    error_code: str | None = None


class EmailProvider(Protocol):
    def send(self, request: EmailDeliveryRequest) -> EmailDeliveryResult: ...


@dataclass(frozen=True, slots=True)
class TencentSESSettings:
    secret_id: str
    secret_key: str
    region: str
    from_email: str
    from_name: str
    verify_email_template_id: int
    activation_test_template_id: int
    quota_alert_template_id: int
    manage_link_template_id: int
    reply_to_addresses: str | None = None

    @classmethod
    def from_environment(cls) -> "TencentSESSettings":
        required = {
            "TENCENTCLOUD_SECRET_ID": os.getenv("TENCENTCLOUD_SECRET_ID", "").strip(),
            "TENCENTCLOUD_SECRET_KEY": os.getenv("TENCENTCLOUD_SECRET_KEY", "").strip(),
            "TENCENT_SES_FROM_EMAIL": os.getenv("TENCENT_SES_FROM_EMAIL", "").strip(),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ValueError("missing Tencent SES settings: " + ", ".join(missing))

        return cls(
            secret_id=required["TENCENTCLOUD_SECRET_ID"],
            secret_key=required["TENCENTCLOUD_SECRET_KEY"],
            region=os.getenv("TENCENT_SES_REGION", "ap-hongkong").strip() or "ap-hongkong",
            from_email=required["TENCENT_SES_FROM_EMAIL"],
            from_name=os.getenv("TENCENT_SES_FROM_NAME", "Appointment Notice").strip() or "Appointment Notice",
            verify_email_template_id=_positive_template_id("TENCENT_SES_VERIFY_EMAIL_TEMPLATE_ID"),
            activation_test_template_id=_positive_template_id("TENCENT_SES_ACTIVATION_TEST_TEMPLATE_ID"),
            quota_alert_template_id=_positive_template_id("TENCENT_SES_QUOTA_ALERT_TEMPLATE_ID"),
            manage_link_template_id=_positive_template_id("TENCENT_SES_MANAGE_LINK_TEMPLATE_ID"),
            reply_to_addresses=os.getenv("TENCENT_SES_REPLY_TO", "").strip() or None,
        )

    def template_id_for(self, template_kind: str) -> int:
        mapping = {
            "verify_email": self.verify_email_template_id,
            "activation_test": self.activation_test_template_id,
            "quota_alert": self.quota_alert_template_id,
            "manage_link": self.manage_link_template_id,
        }
        try:
            return mapping[template_kind]
        except KeyError as exc:
            raise ValueError(f"unsupported template kind: {template_kind}") from exc


def _positive_template_id(name: str) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"missing Tencent SES setting: {name}")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


class TencentSESAPIError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


SESAPITransport = Callable[[dict[str, object]], Mapping[str, object]]


class _TencentCloudSDKTransport:
    """Thin lazy-loading adapter around Tencent Cloud's official Python SDK."""

    def __init__(self, settings: TencentSESSettings) -> None:
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.ses.v20201002 import models, ses_client
        except ImportError as exc:  # pragma: no cover - exercised only in a misconfigured runtime
            raise RuntimeError(
                "Tencent Cloud SES SDK is not installed; install the project runtime dependencies"
            ) from exc

        http_profile = HttpProfile()
        http_profile.endpoint = "ses.tencentcloudapi.com"
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        self._models = models
        self._sdk_exception = TencentCloudSDKException
        self._client = ses_client.SesClient(
            credential.Credential(settings.secret_id, settings.secret_key),
            settings.region,
            client_profile,
        )

    def __call__(self, params: dict[str, object]) -> Mapping[str, object]:
        request = self._models.SendEmailRequest()
        request.from_json_string(json.dumps(params, ensure_ascii=False, separators=(",", ":")))
        try:
            response = self._client.SendEmail(request)
        except self._sdk_exception as exc:
            code = exc.get_code() if hasattr(exc, "get_code") else type(exc).__name__
            raise TencentSESAPIError(str(code), str(exc)) from exc
        return json.loads(response.to_json_string())


_RETRYABLE_CODES = {
    "ClientNetworkError",
    "FailedOperation.FrequencyLimit",
    "FailedOperation.ServiceNotAvailable",
    "InternalError",
    "RequestLimitExceeded",
    "ResourceUnavailable",
}


def _normalized_error_code(code: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", code.casefold()).strip("_")
    return "tencent_ses_" + (normalized or "unknown_error")


class TencentSESEmailProvider:
    """Send transactional template messages through Tencent Cloud SES SendEmail."""

    def __init__(
        self,
        settings: TencentSESSettings,
        *,
        transport: SESAPITransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport or _TencentCloudSDKTransport(settings)

    def send(self, request: EmailDeliveryRequest) -> EmailDeliveryResult:
        params: dict[str, object] = {
            "FromEmailAddress": f"{self.settings.from_name} <{self.settings.from_email}>",
            "Destination": [request.recipient],
            "Subject": request.subject,
            "Template": {
                "TemplateID": self.settings.template_id_for(request.template_kind),
                "TemplateData": json.dumps(
                    dict(request.template_data), ensure_ascii=False, separators=(",", ":")
                ),
            },
            "TriggerType": 1,
            "Unsubscribe": "0",
        }
        if self.settings.reply_to_addresses:
            params["ReplyToAddresses"] = self.settings.reply_to_addresses

        try:
            response = self.transport(params)
        except TencentSESAPIError as exc:
            return EmailDeliveryResult(
                False,
                retryable=exc.code in _RETRYABLE_CODES or exc.code.startswith("InternalError."),
                error_code=_normalized_error_code(exc.code),
            )
        except (TimeoutError, ConnectionError, OSError):
            return EmailDeliveryResult(
                False,
                retryable=True,
                error_code="tencent_ses_network_error",
            )

        message_id = response.get("MessageId")
        if not isinstance(message_id, str) or not message_id.strip():
            return EmailDeliveryResult(
                False,
                retryable=True,
                error_code="tencent_ses_missing_message_id",
            )
        return EmailDeliveryResult(True, provider_message_id=message_id)
