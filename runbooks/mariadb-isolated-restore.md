# Verify an encrypted MariaDB backup in an isolated restore

Use a completed `mariadb-*` output directory from [`backup_mariadb.py`](../scripts/backup_mariadb.py). Each application schema has its own `.sql.gz.age` file. Compare the filenames and ciphertext checksums with the backup receipt before restoring. Use the recovery identity from the agreed custody process. Keep the identity, archive contents and row data out of logs.

Start a fresh MariaDB instance compatible with the backup on an isolated host or container, with no network, no application clients and `event_scheduler=OFF`. Expose only a local socket to the restore process. Copy the archives into a private directory and use private encrypted scratch storage for decrypted files. The off-site destination, retention and cadence are tracked in [Helm #282](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/282).

## Authenticate every archive before importing any schema

Run in Bash with `backup_dir`, `identity_file`, `restore_work_root`, and `restore_socket` set to the absolute paths in the isolated restore environment. The example uses a local account permitted to access the empty instance over its socket.

```bash
set -euo pipefail
shopt -s nullglob
umask 077
: "${backup_dir:?}" "${identity_file:?}" "${restore_work_root:?}" "${restore_socket:?}"
test -S "$restore_socket"
maria=(mariadb --no-defaults --protocol=socket --socket="$restore_socket" --user=root)
test "$("${maria[@]}" --batch --skip-column-names -e 'SELECT @@GLOBAL.event_scheduler')" = OFF
test "$("${maria[@]}" --batch --skip-column-names -e \
  "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME NOT IN \
    ('information_schema','mysql','performance_schema','sys')")" = 0

archives=("$backup_dir"/*.sql.gz.age)
test "${#archives[@]}" -gt 0
scratch=$(mktemp -d "$restore_work_root/mariadb-restore.XXXXXXXX")
trap 'rm -rf -- "$scratch"' EXIT

for archive in "${archives[@]}"; do
    test -f "$archive"
    test ! -L "$archive"
    name=${archive##*/}
    name=${name%.age}
    age --decrypt --identity "$identity_file" --output "$scratch/$name" "$archive"
    gzip -t -- "$scratch/$name"
done

for dump in "$scratch"/*.sql.gz; do
    gzip -cd -- "$dump" | "${maria[@]}"
done
```

The first loop checks the complete ciphertext and the gzip stream for every schema before the second loop can change the instance. Discard the disposable instance after an import failure and start the next attempt with an empty one.

## Record acceptance

- Record the backup run, ciphertext checksums, MariaDB version, start and finish times, and expected and restored schema inventories.
- Compare agreed application invariants with the backup receipt, such as row counts or deterministic hashes. Record pass or fail without exporting application content.
- Each schema has its own single-transaction dump. If the application needs a shared snapshot across schemas, verify those invariants explicitly.
- Keep the scheduled backup and approved application-data restore results in [Helm #533](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/533). The [synthetic MariaDB 11.0.6 rehearsal](https://github.com/OpenResilienceInitiative/ORISO-Helm/actions/runs/44684825809) provides a separate reference for the encrypted backup format.
