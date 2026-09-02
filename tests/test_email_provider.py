import json

import pytest

from app.email_provider import (
    EmailDeliveryRequest,
    TencentSESAPIError,
    TencentSESEmailProvider,
    TencentSESSettings,
)


def _settings() -> TencentSESSettings:
    return TencentSESSettings(
        secret_id="secret-id",
        secret_key="secret-key",
        region="ap-hongkong",
        from_email="notice@example.com",
        from_name="Appointment Notice",
        verify_email_template_id=101,
        activation_test_template_id=102,
        quota_alert_template_id=103,
        manage_link_template_id=104,
    )


def test_tencent_ses_provider_sends_template_request_without_full_url() -> None:
    captured = []

    def transport(params):
        captured.append(params)
        return {"MessageId": "provider-message-1", "RequestId": "request-1"}

    provider = TencentSESEmailProvider(_settings(), transport=transport)
    result = provider.send(EmailDeliveryRequest(
        recipient="user@example.com",
        subject="验证你的预约提醒邮箱",
        template_kind="verify_email",
        template_data={"verify_token": "record.signature"},
    ))

    assert result.accepted and result.provider_message_id == "provider-message-1"
    assert captured[0]["Destination"] == ["user@example.com"]
    assert captured[0]["Template"]["TemplateID"] == 101
    assert json.loads(captured[0]["Template"]["TemplateData"]) == {
        "verify_token": "record.signature"
    }
    assert "http" not in captured[0]["Template"]["TemplateData"]
    assert captured[0]["TriggerType"] == 1


def test_tencent_ses_provider_classifies_retryable_api_failure() -> None:
    def transport(params):
        raise TencentSESAPIError("RequestLimitExceeded", "slow down")

    result = TencentSESEmailProvider(_settings(), transport=transport).send(
        EmailDeliveryRequest(
            recipient="user@example.com",
            subject="预约名额变化提醒",
            template_kind="quota_alert",
            template_data={"office": "RKO", "date": "2026-09-03"},
        )
    )

    assert not result.accepted
    assert result.retryable
    assert result.error_code == "tencent_ses_requestlimitexceeded"


def test_tencent_ses_provider_treats_template_rejection_as_permanent() -> None:
    def transport(params):
        raise TencentSESAPIError("FailedOperation.TemplateNotApproved", "not approved")

    result = TencentSESEmailProvider(_settings(), transport=transport).send(
        EmailDeliveryRequest(
            recipient="user@example.com",
            subject="预约名额变化提醒",
            template_kind="quota_alert",
            template_data={},
        )
    )

    assert not result.accepted
    assert not result.retryable
    assert result.error_code == "tencent_ses_failedoperation_templatenotapproved"


def test_tencent_ses_settings_require_all_template_ids(monkeypatch) -> None:
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "id")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "key")
    monkeypatch.setenv("TENCENT_SES_FROM_EMAIL", "notice@example.com")
    monkeypatch.delenv("TENCENT_SES_VERIFY_EMAIL_TEMPLATE_ID", raising=False)

    with pytest.raises(ValueError, match="TENCENT_SES_VERIFY_EMAIL_TEMPLATE_ID"):
        TencentSESSettings.from_environment()
