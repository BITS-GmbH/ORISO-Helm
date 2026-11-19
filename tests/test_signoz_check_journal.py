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
