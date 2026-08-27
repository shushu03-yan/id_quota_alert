# Deployment preparation (not production approval)

`deploy/systemd/` contains three single-host service templates:

- `hkid-poller.service`: the only shared GovHK public quota poller;
- `hkid-email-worker.service`: matcher + notification outbox email worker;
- `hkid-web.service`: local WSGI activation and management MVP, bound to loopback for a future HTTPS reverse proxy.

All services use the non-root `hkid-alert` account, `/opt/hkid-quota-alert` as their working directory, and `/etc/hkid-quota-alert.env` for configuration. Secrets must not be placed in the unit files or committed to Git.

Before any local pilot, an operator still needs to create the user/directories, install the app in a virtual environment, provision a real environment file with restrictive permissions, configure HTTPS, test the actual Email Provider, run the 3–7 day soak, exercise stop/restart recovery, and restore a backup into a separate database. These templates have not been deployed or production-validated.
