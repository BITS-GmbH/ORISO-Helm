#!/usr/bin/env python3
"""Read completed external Dev SigNoz checks from the systemd manager journal."""

import argparse
from datetime import datetime, timedelta, timezone
from itertools import islice
import json
from pathlib import Path
import re
import subprocess
import sys
import time


UPTIME = "oriso-signoz-uptime.service"
HEARTBEAT = "oriso-signoz-heartbeat-check.service"
JOB_DONE = "39f53479d3a045ac8e11786248231fbf"
JOB_FAILED = "be02cf6855d2428ba40df7e9d022f03d"
UNIT_SUCCEEDED = "7ad2d189f7e94e70a38c781354912448"
UNIT_FAILED = "d9b373ed55a64feb8242e02dbe79a49c"
PROCESS_EXIT = "98e322203f7a4ed290d09fe03c09fe15"
PAGE_SIZE = 24
MAX_COMPLETION_AGE_SECONDS = 180
UNIT_MATCHES = ("_PID=1", f"UNIT={UPTIME}", f"UNIT={HEARTBEAT}")
CADENCE_UNITS = {"uptime": UPTIME, "heartbeat": HEARTBEAT}
CADENCE_EXIT = {"OK": 0, "CRITICAL": 2, "UNKNOWN": 3}
PLUGIN_STATES = {
    UPTIME: {1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"},
    HEARTBEAT: {2: "CRITICAL"},
}
JOURNAL_FIELDS = (
    "UNIT,_PID,_MACHINE_ID,INVOCATION_ID,JOB_TYPE,JOB_RESULT,MESSAGE_ID,"
    "UNIT_RESULT,EXIT_CODE,EXIT_STATUS"
)


def read_journal(options, matches, limit):
    """Read one bounded journal page in forward order."""
    machine = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    if not re.fullmatch("[0-9a-f]{32}", machine):
        raise ValueError("invalid local machine ID")
    command = [
        "/usr/bin/timeout", "--kill-after=1s", "8s",
        "/usr/bin/journalctl", "--system", "--quiet", "--no-pager",
        "--output=json", f"--output-fields={JOURNAL_FIELDS}", *options,
        f"_MACHINE_ID={machine}", *matches,
    ]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          encoding="utf-8") as process:
        try:
            lines = list(islice(process.stdout, limit + 1))
            more = len(lines) > limit
            if not more and process.wait() != 0:
                raise ValueError("system journal read failed")
            return [json.loads(line) for line in lines[:limit]], more
        finally:
            if process.poll() is None:
                process.terminate()


def classify(completion, details):
    identity = ("_PID", "_MACHINE_ID", "_BOOT_ID", "UNIT", "INVOCATION_ID")
    entries = [
        row for row in details
        if all(row.get(key) == completion.get(key) for key in identity)
    ]
    successes = [row for row in entries if row["MESSAGE_ID"] == UNIT_SUCCEEDED]
    failures = [row for row in entries if row["MESSAGE_ID"] == UNIT_FAILED]
    exits = [row for row in entries if row["MESSAGE_ID"] == PROCESS_EXIT]
    clean_exit = not exits or (
        len(exits) == 1 and exits[0]["EXIT_CODE"] == "exited" and exits[0]["EXIT_STATUS"] == "0"
    )
    if completion["JOB_RESULT"] == "done" and len(successes) == 1 and not failures and clean_exit:
        # These two oneshots use the default successful exit status of zero.
        return "OK", "success", 0
    result = failures[0]["UNIT_RESULT"] if len(failures) == 1 else None
    status = None
    if len(exits) == 1 and exits[0]["EXIT_CODE"] == "exited":
        status = int(exits[0]["EXIT_STATUS"])
    state = "UNKNOWN"
    if completion["JOB_RESULT"] == "failed" and result == "exit-code" and not successes:
        state = PLUGIN_STATES[completion["UNIT"]].get(status, "UNKNOWN")
    return state, result, status


def invocation_ids(rows):
    values = {row.get("INVOCATION_ID") for row in rows}
    return sorted(value for value in values if isinstance(value, str) and re.fullmatch("[0-9a-f]{32}", value))


def completion_details(ids, *, options=(), limit=PAGE_SIZE * 4):
    if not ids:
        return []
    details, extra = read_journal(options, (
        *UNIT_MATCHES, *(f"INVOCATION_ID={value}" for value in ids),
        f"MESSAGE_ID={UNIT_SUCCEEDED}", f"MESSAGE_ID={UNIT_FAILED}", f"MESSAGE_ID={PROCESS_EXIT}",
    ), limit)
    if extra:
        raise ValueError("too many matching systemd result records")
    return details


def read_page(*, cursor=None, since=None):
    options = [f"--cursor={cursor}"] if cursor is not None else [f"--since={since}"]
    match = (*UNIT_MATCHES, "JOB_TYPE=start", f"MESSAGE_ID={JOB_DONE}", f"MESSAGE_ID={JOB_FAILED}")
    rows, more = read_journal(options, match, PAGE_SIZE + int(cursor is not None))
    if cursor is not None:
        if not rows or rows[0]["__CURSOR"] != cursor:
            raise ValueError("cannot resume cursor in system journal")
        rows = rows[1:]

    ids = invocation_ids(rows)
    details = completion_details(ids)

    completed = []
    for row in rows:
        identity = row.get("INVOCATION_ID")
        state, result, status = classify(row, details) if identity in ids else ("UNKNOWN", None, None)
        timestamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=int(row["__REALTIME_TIMESTAMP"])
        )
        completed.append({
            "unit": row["UNIT"], "state": state, "service_result": result,
            "exit_status": status, "invocation_id": identity,
            "machine_id": row["_MACHINE_ID"], "boot_id": row["_BOOT_ID"],
            "completed_at": timestamp.isoformat(),
            "cursor": row["__CURSOR"],
        })
    return {"completed": completed, "next_cursor": rows[-1]["__CURSOR"] if rows else cursor, "more": more}


def timer_state(timer):
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", "--no-pager", "--property=ActiveState", "--value", timer],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8", timeout=4,
    )
    state = result.stdout.strip()
    if state not in {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance"}:
        raise ValueError("invalid systemd timer state")
    return state


def local_boot_id():
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip().replace("-", "")
    if not re.fullmatch("[0-9a-f]{32}", boot_id):
        raise ValueError("invalid current boot ID")
    return boot_id


def check_cadence(unit):
    result = {"unit": unit}
    try:
        if timer_state(unit.removesuffix(".service") + ".timer") != "active":
            return dict(result, state="CRITICAL", reason="timer-not-active")
        boot_id = local_boot_id()
        rows, extra = read_journal((f"--boot={boot_id}", "--lines=1"), (
            "_PID=1", f"UNIT={unit}", "JOB_TYPE=start", f"MESSAGE_ID={JOB_DONE}", f"MESSAGE_ID={JOB_FAILED}",
        ), 1)
        if not rows and not extra:
            return dict(result, state="CRITICAL", reason="missing-completion")
        if extra or len(rows) != 1 or rows[0]["UNIT"] != unit or rows[0]["_BOOT_ID"] != boot_id:
            raise ValueError("unexpected completion record")
        completion = rows[0]
        timestamp = int(completion["__MONOTONIC_TIMESTAMP"])
        age = time.monotonic_ns() // 1000 - timestamp
        if timestamp <= 0 or age < 0:
            raise ValueError("invalid current-boot timestamp")
        result["age_seconds"] = age / 1_000_000
        if age >= MAX_COMPLETION_AGE_SECONDS * 1_000_000:
            return dict(result, state="CRITICAL", reason="stale-completion")
        ids = invocation_ids(rows)
        details = completion_details(ids, options=(f"--boot={boot_id}",), limit=4)
        probe_state, service_result, _ = classify(completion, details) if ids else ("UNKNOWN", None, None)
        if service_result is None:
            return dict(result, state="UNKNOWN", reason="unattributed-completion")
        return dict(result, state="OK", reason="recent-completion", last_probe_state=probe_state)
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        return dict(result, state="UNKNOWN", reason="invalid-runner-state")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    start = parser.add_mutually_exclusive_group(required=True)
    start.add_argument("--since", help="Journal start time for the first page")
    start.add_argument("--cursor", help="Last completion cursor accepted by the consumer")
    start.add_argument("--check-cadence", choices=CADENCE_UNITS, help="Check a running timer and its latest completion")
    args = parser.parse_args()
    if args.check_cadence:
        result = check_cadence(CADENCE_UNITS[args.check_cadence])
        print(json.dumps(result, sort_keys=True))
        return CADENCE_EXIT[result["state"]]
    try:
        page = read_page(since=args.since, cursor=args.cursor)
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"Cannot read SigNoz check completions: {error}", file=sys.stderr)
        return 3
    print(json.dumps(page, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
