# Nightly MariaDB backup on the Dev master

The Dev master runs a nightly logical backup of the Helm MariaDB StatefulSet with [`backup_mariadb.py`](../scripts/backup_mariadb.py). The script checks that `pod/mariadb-0` is ready before it reads anything. It writes one compressed dump per application schema into a private staging folder and publishes that folder only after every schema succeeded. A failed run leaves no folder behind.

This replaces the hand-installed `/root/db-backups/backup-mariadb.sh` cron job, which is not in Git. That job pinned the pod name `oriso-mariadb-0` and called `mysql`/`mysqldump`. The chart names the pod `mariadb-0`, and the MariaDB 11.0.6 image ships only `mariadb`/`mariadb-dump`. Every run from 14 July to 21 November 2026 therefore failed and left an empty dated folder. `backup_mariadb.py` uses `pod/mariadb-0` and the `mariadb`/`mariadb-dump` clients. `tests/test_mariadb_host_backup_units.py` fails the chart CI if the script's pod name and the rendered StatefulSet drift apart again.

On 21 November 2026 the legacy script was repaired in place as an interim fix (copy of the old version: `/root/db-backups/backup-mariadb.sh.bak-20261121`). Its first good run is `/root/db-backups/mariadb/20261121_070458`. The last complete July nightlies (`20260709`–`20260713`) were copied to `/root/db-backups/archive-nightly-2026-07`, so the legacy job's keep-latest-5 clean-up cannot delete them. Keep that archive folder until the retention decision under #282 says otherwise.

The legacy job and this timer both run at 02:00 UTC. Run only one of them: retire the legacy cron line in the same step as enabling the timer, as shown below.

Installing or changing this on Dev is an operator change on the master. Do it only with explicit approval for that task. Off-site storage, encryption key custody, retention and restore cadence stay under [Helm #282](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/282) and [Helm #533](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/533).

## Install

Run on the Dev master as root, from a checkout of the reviewed `dev` commit.

```bash
set -euo pipefail
install -d -m 0700 /opt/oriso/mariadb-backup /root/db-backups/mariadb-helm
install -m 0600 scripts/backup_mariadb.py /opt/oriso/mariadb-backup/backup_mariadb.py
install -m 0644 runbooks/systemd/oriso-mariadb-backup.service runbooks/systemd/oriso-mariadb-backup.timer /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/oriso-mariadb-backup.service /etc/systemd/system/oriso-mariadb-backup.timer
systemctl daemon-reload

# Read-only preflight: confirms the ready pod without opening MariaDB.
python3 -I /opt/oriso/mariadb-backup/backup_mariadb.py /etc/rancher/k3s/k3s.yaml default caritas --check-target
```

Run one backup by hand, then check that a complete folder appeared:

```bash
systemctl start oriso-mariadb-backup.service
systemctl show oriso-mariadb-backup.service -p Result -p ExecMainStatus
ls -l "$(ls -1d /root/db-backups/mariadb-helm/mariadb-* | tail -1)"
```

Every application schema must have a non-empty `.sql.gz` file. Then retire the legacy job and enable the timer:

```bash
crontab -l > /root/db-backups/crontab.before-oriso-mariadb-backup
crontab -l | grep -v '/root/db-backups/backup-mariadb.sh' | crontab -
systemctl enable --now oriso-mariadb-backup.timer
systemctl list-timers oriso-mariadb-backup.timer --no-pager
```

Keep the existing dumps in `/root/db-backups`, including `mariadb/` from the legacy job and `archive-nightly-2026-07/`, until a restore from the new folder has been verified with [the isolated restore runbook](mariadb-isolated-restore.md).

## Check the next morning

```bash
systemctl show oriso-mariadb-backup.service -p Result -p ExecMainExitTimestamp
journalctl -u oriso-mariadb-backup.service --since yesterday --no-pager | tail -20
```

`Result=success` and a new `mariadb-*` folder dated today mean the run is complete. On failure, the journal names the step: the target check, schema discovery, or a named schema dump.

## Encryption

Once the recovery identity custody under #282 is agreed, install `age` and a recipients file on the master. Then switch the service to encrypted output with a drop-in, so dumps never land in plain text:

```ini
# /etc/systemd/system/oriso-mariadb-backup.service.d/encrypt.conf
[Service]
ExecStart=
ExecStart=/usr/bin/python3 -I /opt/oriso/mariadb-backup/backup_mariadb.py /etc/rancher/k3s/k3s.yaml default caritas /root/db-backups/mariadb-helm --age-recipients-file /etc/oriso/mariadb-backup.age-recipients
```

## Not covered yet

- Retention. The script keeps every run. One Dev run is about 2 MB, so local disk is not a near-term risk, but pruning belongs with the retention decision in #282.
- Off-site copy to the Storage Box (#533).
- MongoDB and Matrix PostgreSQL. Their restore rehearsals are in #606 and #608.
