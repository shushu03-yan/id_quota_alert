"""Provider-neutral email contract and a development SMTP implementation."""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
import os
import smtplib
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EmailDeliveryResult:
    accepted: bool
    provider_message_id: str | None = None
    retryable: bool = False
    error_code: str | None = None


class EmailProvider(Protocol):
    def send(self, *, recipient: str, subject: str, text: str) -> EmailDeliveryResult: ...


@dataclass(frozen=True, slots=True)
class SMTPSettings:
    host: str
    port: int
    username: str | None
    password: str | None
    from_email: str
    from_name: str = "ID Quota Alert"
    use_tls: bool = True

    @classmethod
    def from_environment(cls) -> "SMTPSettings":
        host = os.getenv("SMTP_HOST", "").strip()
        from_email = os.getenv("SMTP_FROM_EMAIL", "").strip()
        if not host or not from_email:
            raise ValueError("SMTP_HOST and SMTP_FROM_EMAIL are required")
        password = os.getenv("SMTP_PASSWORD") or os.getenv("SMTP_APP_PASSWORD")
        return cls(
            host=host,
            port=int(os.getenv("SMTP_PORT", "587")),
            username=os.getenv("SMTP_USERNAME") or None,
            password=password or None,
            from_email=from_email,
            from_name=os.getenv("SMTP_FROM_NAME", "ID Quota Alert"),
            use_tls=os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"},
        )


class SMTPEmailProvider:
    """Small SMTP adapter intended for development and provider smoke tests."""

    def __init__(self, settings: SMTPSettings, *, timeout_seconds: float = 20.0) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds

    def send(self, *, recipient: str, subject: str, text: str) -> EmailDeliveryResult:
        message = EmailMessage()
        message["From"] = f"{self.settings.from_name} <{self.settings.from_email}>"
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = make_msgid(domain=self.settings.from_email.split("@")[-1])
        message.set_content(text)
        try:
            with smtplib.SMTP(self.settings.host, self.settings.port, timeout=self.timeout_seconds) as client:
                if self.settings.use_tls:
                    client.starttls()
                if self.settings.username:
                    client.login(self.settings.username, self.settings.password or "")
                refused = client.send_message(message)
            if refused:
                return EmailDeliveryResult(False, retryable=False, error_code="smtp_recipient_refused")
            return EmailDeliveryResult(True, provider_message_id=message.get("Message-ID"))
        except (TimeoutError, ConnectionError, smtplib.SMTPServerDisconnected):
            return EmailDeliveryResult(False, retryable=True, error_code="smtp_connection_error")
        except smtplib.SMTPResponseException as exc:
            retryable = 400 <= exc.smtp_code < 500
            return EmailDeliveryResult(False, retryable=retryable, error_code=f"smtp_{exc.smtp_code}")
        except OSError:
            return EmailDeliveryResult(False, retryable=True, error_code="smtp_connection_error")
        except smtplib.SMTPException:
            return EmailDeliveryResult(False, retryable=False, error_code="smtp_error")
