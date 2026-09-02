# Tencent Cloud SES templates

These files are the upload-ready message bodies for the four transactional
notification kinds used by the application. Upload the HTML and plain-text content
without changing variable names.

| Application kind | Suggested Tencent template name | Subject used by code | Variables |
|---|---|---|---|
| `verify_email` | 郵箱驗證通知 | 驗證你的預約提醒郵箱 | `verify_token` |
| `activation_test` | 服務啟用確認 | 你的預約提醒服務已啟動 | `plan_name`, `starts_on`, `expires_on`, `target_count` |
| `quota_alert` | 預約名額變化通知 | 預約名額變化提醒 | `office`, `date`, `availability`, `detected_at` |
| `manage_link` | 管理連結通知 | 管理你的預約提醒 | `manage_token` |

## Fixed-link policy

- `verify_email` hardcodes `https://hkid-notice.com/verify?token=` and substitutes only `{{verify_token}}`.
- `manage_link` hardcodes `https://hkid-notice.com/manage?token=` and substitutes only `{{manage_token}}`.
- `quota_alert` hardcodes the GovHK booking information page and has no URL variable.
- `activation_test` contains only a fixed management-request URL.

## Suggested summaries

- 郵箱驗證通知：用戶主動申請預約提醒後的郵箱所有權驗證郵件。
- 服務啟用確認：用戶完成郵箱驗證後的服務啟用與投遞鏈路確認郵件；不是名額提醒。
- 預約名額變化通知：向已啟用提醒的用戶通知符合其設定的公開預約名額變化。
- 管理連結通知：用戶主動申請後發送的一次性提醒管理連結。

## Review checklist

- Keep the service name `Appointment Notice` / `預約提醒`; do not use `HKID` as the brand name.
- Do not add government logos, seals, emblems, or language implying official status.
- Do not replace any fixed link with a complete-URL variable.
- Keep the independent-service disclaimer in every template.
- Confirm that the sender address and sender alias use the verified sending domain.
- Submit all four templates and wait for approval before enabling the email worker.
