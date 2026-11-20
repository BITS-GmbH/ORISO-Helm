"""Exercise completion, failure detail and cursor handling at the reader boundary."""

from unittest import TestCase
from unittest.mock import patch

from scripts import signoz_check_journal as reader


BOOT = "a" * 32
INVOCATION = "b" * 32
MACHINE = "d" * 32
UPTIME = "oriso-signoz-uptime.service"
HEARTBEAT = "oriso-signoz-heartbeat-check.service"


def event(message, **fields):
    return {
        "_PID": "1", "_MACHINE_ID": MACHINE, "_BOOT_ID": BOOT, "UNIT": UPTIME,
        "INVOCATION_ID": INVOCATION, "MESSAGE_ID": message, **fields,
    }


class CheckJournalTest(TestCase):
    def test_success_requires_a_clean_completion_for_this_invocation(self):
        completed = event(reader.JOB_DONE, JOB_RESULT="done")
        self.assertEqual(reader.classify(completed, []), ("UNKNOWN", None, None))
        unrelated = event(reader.UNIT_SUCCEEDED, INVOCATION_ID="c" * 32)
        self.assertEqual(reader.classify(completed, [unrelated]), ("UNKNOWN", None, None))
        foreign = event(reader.UNIT_SUCCEEDED, _MACHINE_ID="e" * 32)
        self.assertEqual(reader.classify(completed, [foreign]), ("UNKNOWN", None, None))
        clean = event(reader.UNIT_SUCCEEDED)
        self.assertEqual(reader.classify(completed, [clean]), ("OK", "success", 0))

    def test_plugin_exit_and_runner_failures_remain_distinct(self):
        completed = event(reader.JOB_FAILED, JOB_RESULT="failed")
        exited = event(reader.PROCESS_EXIT, EXIT_CODE="exited", EXIT_STATUS="2")
        failed = event(reader.UNIT_FAILED, UNIT_RESULT="exit-code")
        self.assertEqual(reader.classify(completed, [exited, failed]), ("CRITICAL", "exit-code", 2))
        missing = {**exited, "EXIT_STATUS": "203"}
        self.assertEqual(reader.classify(completed, [missing, failed]), ("UNKNOWN", "exit-code", 203))
        timed_out = {**failed, "UNIT_RESULT": "timeout"}
        killed = {**exited, "EXIT_CODE": "killed", "EXIT_STATUS": "9"}
        self.assertEqual(reader.classify(completed, [killed, timed_out]), ("UNKNOWN", "timeout", None))

    def test_pages_keep_failed_runs_and_resume_after_the_last_returned_record(self):
        first = event(
            reader.JOB_FAILED, JOB_TYPE="start", JOB_RESULT="failed",
            __CURSOR="run-a", __REALTIME_TIMESTAMP="5000000",
        )
        second = event(
            reader.JOB_DONE, JOB_TYPE="start", JOB_RESULT="done", UNIT=HEARTBEAT,
            INVOCATION_ID="c" * 32, __CURSOR="run-b", __REALTIME_TIMESTAMP="6000000",
        )
        failed = event(reader.UNIT_FAILED, UNIT_RESULT="timeout")
        clean = event(reader.UNIT_SUCCEEDED, UNIT=HEARTBEAT, INVOCATION_ID="c" * 32)
        with patch.object(reader, "read_journal", side_effect=[
            ([first, second], False), ([failed, clean], False),
        ]):
            page = reader.read_page(since="10 minutes ago")
        self.assertEqual([run["state"] for run in page["completed"]], ["UNKNOWN", "OK"])
        self.assertEqual(page["next_cursor"], "run-b")

        with patch.object(reader, "read_journal", return_value=([first], False)):
            with self.assertRaisesRegex(ValueError, "resume cursor"):
                reader.read_page(cursor="removed-record")

        with patch.object(reader, "read_journal", return_value=([second], False)):
            page = reader.read_page(cursor="run-b")
        self.assertEqual(page, {"completed": [], "next_cursor": "run-b", "more": False})


class CheckCadenceTest(TestCase):
    def test_recent_failed_probe_keeps_its_result_and_checks_heartbeat_only(self):
        completed = event(reader.JOB_FAILED, UNIT=HEARTBEAT, JOB_RESULT="failed",
                          __MONOTONIC_TIMESTAMP="100000000")
        failed = event(reader.UNIT_FAILED, UNIT=HEARTBEAT, UNIT_RESULT="exit-code")
        exited = event(reader.PROCESS_EXIT, UNIT=HEARTBEAT, EXIT_CODE="exited", EXIT_STATUS="2")
        with (
            patch.object(reader, "timer_state", return_value="active") as timer,
            patch.object(reader, "local_boot_id", return_value=BOOT),
            patch.object(reader.time, "monotonic_ns", return_value=140_000_000_000),
            patch.object(reader, "read_journal", side_effect=[([completed], False), ([failed, exited], False)]) as journal,
        ):
            result = reader.check_cadence(HEARTBEAT)
        timer.assert_called_once_with("oriso-signoz-heartbeat-check.timer")
        options, matches, limit = journal.call_args_list[0].args
        self.assertIn(f"--boot={BOOT}", options)
        self.assertIn(f"UNIT={HEARTBEAT}", matches)
        self.assertNotIn(f"UNIT={UPTIME}", matches)
        self.assertEqual(limit, 1)
        self.assertEqual(result, {
            "unit": HEARTBEAT, "state": "OK", "reason": "recent-completion",
            "age_seconds": 40.0, "last_probe_state": "CRITICAL",
        })

    def test_completion_at_deadline_is_stale(self):
        completed = event(reader.JOB_DONE, JOB_RESULT="done", __MONOTONIC_TIMESTAMP="100000000")
        with (
            patch.object(reader, "timer_state", return_value="active"),
            patch.object(reader, "local_boot_id", return_value=BOOT),
            patch.object(reader.time, "monotonic_ns", return_value=280_000_000_000),
            patch.object(reader, "read_journal", return_value=([completed], False)),
        ):
            result = reader.check_cadence(UPTIME)
        self.assertEqual((result["state"], result["reason"]), ("CRITICAL", "stale-completion"))
        self.assertEqual(result["age_seconds"], 180)

    def test_stopped_timer_is_critical_before_journal_lookup(self):
        with (
            patch.object(reader, "timer_state", return_value="inactive"),
            patch.object(reader, "read_journal") as journal,
        ):
            result = reader.check_cadence(UPTIME)
        journal.assert_not_called()
        self.assertEqual((result["state"], result["reason"]), ("CRITICAL", "timer-not-active"))

    def test_no_current_run_and_unverifiable_current_run_fail_closed(self):
        completed = event(reader.JOB_DONE, JOB_RESULT="done", __MONOTONIC_TIMESTAMP="100000000")
        for label, records, expected in (
            ("empty", [], ("CRITICAL", "missing-completion")),
            ("old boot", [{**completed, "_BOOT_ID": "c" * 32}], ("UNKNOWN", "invalid-runner-state")),
            ("wrong unit", [{**completed, "UNIT": HEARTBEAT}], ("UNKNOWN", "invalid-runner-state")),
            ("future timestamp", [{**completed, "__MONOTONIC_TIMESTAMP": "200000000"}],
             ("UNKNOWN", "invalid-runner-state")),
            ("invalid timestamp", [{**completed, "__MONOTONIC_TIMESTAMP": "beep"}],
             ("UNKNOWN", "invalid-runner-state")),
            ("skipped condition", [{**completed, "INVOCATION_ID": None}],
             ("UNKNOWN", "unattributed-completion")),
            ("no service result", [completed], ("UNKNOWN", "unattributed-completion")),
        ):
            with (
                self.subTest(label=label),
                patch.object(reader, "timer_state", return_value="active"),
                patch.object(reader, "local_boot_id", return_value=BOOT),
                patch.object(reader.time, "monotonic_ns", return_value=140_000_000_000),
                patch.object(reader, "read_journal", side_effect=[(records, False), ([], False)]),
            ):
                result = reader.check_cadence(UPTIME)
                self.assertEqual((result["state"], result["reason"]), expected)
