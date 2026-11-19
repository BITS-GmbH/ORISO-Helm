#!/usr/bin/env python3
"""Stage and publish one local logical backup of the Helm MariaDB StatefulSet."""

import argparse
from datetime import datetime, timezone
import gzip
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


SYSTEM_SCHEMAS = {"information_schema", "mysql", "performance_schema", "sys"}
POD = "pod/mariadb-0"
POD_READINESS = 'jsonpath={.metadata.uid} {.status.conditions[?(@.type=="Ready")].status}'
AUTH = (
    ': "${MYSQL_ROOT_PASSWORD:?MariaDB credential is unavailable}"; '
    'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; '
)
DISCOVER = AUTH + 'exec mariadb --user=root --batch --skip-column-names --execute "SHOW DATABASES"'
DUMP = (
    AUTH + "exec mariadb-dump --user=root --single-transaction --routines "
    '--triggers --events --databases "$1"'
)


def save_dump(command, path):
    with subprocess.Popen(command, stdout=subprocess.PIPE) as dump:
        with gzip.open(path, "xb") as output:
            first_byte = dump.stdout.read(1)
            if first_byte:
                output.write(first_byte)
                shutil.copyfileobj(dump.stdout, output)
        code = dump.wait()
        if code:
            raise RuntimeError(f"MariaDB dump for {path.name} failed with exit status {code}")
        if not first_byte:
            raise RuntimeError(f"MariaDB dump for {path.name} was empty")


def wait_for_target(kubernetes):
    subprocess.run(
        kubernetes + ["wait", "--for=condition=Ready", POD, "--timeout=30s"],
        check=True,
    )


def check_target(kubernetes):
    wait_for_target(kubernetes)
    fields = subprocess.check_output(
        kubernetes + ["get", POD, "-o", POD_READINESS],
        text=True,
    ).split()
    if len(fields) != 2 or fields[1] != "True":
        raise RuntimeError("Could not confirm the identity of the ready MariaDB pod")
    return fields[0]


def backup(kubernetes, output_root):
    wait_for_target(kubernetes)
    remote = kubernetes + ["exec", POD, "--container", "mariadb", "--", "sh", "-ec"]
    listed_schemas = subprocess.check_output(remote + [DISCOVER], text=True)
    schemas = [
        name for name in listed_schemas.splitlines()
        if name and name.casefold() not in SYSTEM_SCHEMAS
    ]
    if not schemas:
        raise RuntimeError("MariaDB returned no application schemas")
    if any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_$-]*", name) for name in schemas):
        raise RuntimeError("MariaDB returned a schema name unsuitable for a backup filename")

    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output_root.is_symlink() or output_root.stat().st_mode & 0o077:
        raise RuntimeError("Use a private backup directory with no group or world permissions")
    started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with TemporaryDirectory(prefix=f".mariadb-{started}-", dir=output_root) as scratch:
        stage = Path(scratch)
        for schema in schemas:
            save_dump(remote + [DUMP, "mariadb-dump", schema], stage / f"{schema}.sql.gz")
        destination = output_root / stage.name.removeprefix(".")
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Backup destination already exists")
        stage.rename(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kubeconfig", type=Path)
    parser.add_argument("context")
    parser.add_argument("namespace")
    parser.add_argument("output_root", type=Path, nargs="?", help="private local directory for backups")
    parser.add_argument(
        "--check-target", action="store_true", help="report the ready pod without accessing MariaDB",
    )
    args = parser.parse_args()
    if not args.kubeconfig.is_file():
        parser.error("kubeconfig must be an existing file")
    if not args.check_target and args.output_root is None:
        parser.error("output_root is required for a backup")
    kubernetes = [
        "kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context,
        "--namespace", args.namespace,
    ]
    os.umask(0o077)
    operation = "Target check" if args.check_target else "Backup"
    try:
        if args.check_target:
            uid = check_target(kubernetes)
            checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"{checked_at} MariaDB target ready context={args.context} "
                  f"namespace={args.namespace} {POD} uid={uid}")
            return 0
        destination = backup(kubernetes, args.output_root)
    except subprocess.CalledProcessError as error:
        print(f"{operation} stopped after kubectl exited with status {error.returncode}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as error:
        print(f"{operation} stopped: {error}", file=sys.stderr)
        return 1
    print(f"Completed local MariaDB backup: {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
