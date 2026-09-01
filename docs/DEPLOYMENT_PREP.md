# Deployment preparation (not production approval)

`deploy/systemd/` now contains the single-host launch skeleton:

- `hkid-poller.service`: the only shared GovHK public quota poller;
- `hkid-email-worker.service`: matcher + notification outbox email worker;
- `hkid-web.service`: activation, Email verification, management-link request, target management and unsubscribe Web app;
- `hkid-maintenance.service`: one-shot subscription maintenance command;
- `hkid-maintenance.timer`: runs maintenance hourly so Goal / Family zero-match extensions are actually evaluated at runtime.
- `hkid-backup.service` + `hkid-backup.timer`: create and retain a daily consistent SQLite backup.

All services use the non-root `hkid-alert` account, `/opt/hkid-quota-alert` as their working directory, and `/etc/hkid-quota-alert.env` for configuration. Secrets must not be placed in unit files or committed to Git.

The Web unit uses Gunicorn on `127.0.0.1:8080`. The Nginx template in
`deploy/nginx/hkid-notice.conf` provides the public reverse proxy, request-size limit
and basic rate limiting for `hkid-notice.com` and `www.hkid-notice.com`; HTTPS
certificates are provisioned on the host after DNS is confirmed.

## Minimal operator commands

Before deployment, the same flows can be exercised locally:

```powershell
python -m app health
python -m app soak-summary
python -m app email-smoke --to your-test@example.com
python -m app activation-code create --plan goal
python -m app customer list
python -m app subscription list
python -m app subscription show 1
python -m app outbox status
python -m app maintenance
python -m app backup --retain 30
```

`email-smoke` is intentionally a direct provider smoke test. It does not create a quota alert and its message explicitly says that it does not indicate appointment availability.

## Web launch flow

The Web skeleton now supports:

```text
/
  -> /activate
     -> verification email
        -> /verify
           -> activation confirmation email

/manage/request
  -> management email
     -> /manage
        -> update targets OR unsubscribe
```

The activation and management pages use ordinary date, office and status form controls; the previous JSON POST format remains accepted internally for backwards-compatible tests/debugging, but is no longer the user-facing interface.

The current public target form exposes up to six targets (sufficient for Trial / Quick / Goal). Family remains technically supported in the domain model but is still intended for manual/hidden V1 handling.

## Still required before a public paid launch

These templates are not a production approval. An operator still needs to:

- create the Linux user/directories and Python virtual environment;
- provision `/etc/hkid-quota-alert.env` with restrictive permissions;
- provision and renew the HTTPS certificate for the configured domain;
- validate the real Email Provider and sender-domain authentication;
- continue and review the 3–7 day source/poller soak;
- exercise stop/restart recovery;
- restore a backup into a separate database and verify it;
- decide offsite backup and monitoring;
- finalize Privacy, Terms and Refund text (the Web routes are explicitly marked as pilot drafts);
- confirm the source/third-party alert/commercial-use boundaries before public charging.
