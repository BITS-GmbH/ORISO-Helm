"""Exercise the MongoDB and Synapse PostgreSQL backup writer against disposable kubectl and age fixtures."""

import gzip
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest

from render_signoz_dev_observability_test import by_key, render


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backup_datastores.py"
RECIPIENT = "age1" + "q" * 58
KUBECTL = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
open(os.environ["BACKUP_TEST_CALLS"], "a").write(json.dumps(args) + "\\n")
action = args[6]
scenario = os.environ["BACKUP_TEST_SCENARIO"]
if action == "wait":
    sys.exit(1 if scenario == "missing-pod" else 0)
if action == "get":
    print("synthetic-pod-uid", "False" if scenario == "not-ready" else "True")
    sys.exit(0)
if scenario == "empty-dump":
    sys.exit(0)
sys.stdout.write("synthetic dump of " + args[7 + 1] + "\\n")
sys.exit(1 if scenario == "failed-dump" else 0)
"""
# The fixture "encrypts" by prefixing a marker, so a test can prove the stream went through age.
AGE = """#!/usr/bin/env python3
import sys
sys.stdout.buffer.write(b"AGE-FIXTURE\\n" + sys.stdin.buffer.read())
"""


class DatastoreBackupTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.output = root / "backups"
        self.calls = root / "calls.jsonl"
        self.recipients = root / "recipients.txt"
        self.recipients.write_text(f"# test\n{RECIPIENT}\n")
        self.kubeconfig = root / "kubeconfig"
        self.kubeconfig.write_text("apiVersion: v1\n")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, body in (("kubectl", KUBECTL), ("age", AGE)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o700)
        self.bin_dir = bin_dir

    def run_backup(self, datastore, scenario="ok", *options, with_output=True):
        env = {
            **os.environ, "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BACKUP_TEST_CALLS": str(self.calls), "BACKUP_TEST_SCENARIO": scenario,
        }
        command = [sys.executable, str(SCRIPT), str(self.kubeconfig), "audit-dev", "caritas", datastore]
        if with_output:
            command.append(str(self.output))
        return subprocess.run(command + list(options), env=env, text=True, capture_output=True, timeout=30)

    def published(self):
        return sorted(p.name for p in self.output.iterdir()) if self.output.exists() else []

    def test_pods_match_the_rendered_dev_statefulsets(self):
        rendered = by_key(render("values-dev.yaml"))
        script = SCRIPT.read_text()
        pods = re.findall(r'"pod": "pod/([^"]+)"', script)
        self.assertEqual(sorted(pods), ["matrix-postgres-0", "mongodb-0"])
        for pod in pods:
            self.assertIn(("StatefulSet", pod.rsplit("-", 1)[0]), rendered)

    def test_each_datastore_publishes_one_encrypted_run(self):
        expected = {"mongodb": "mongodb.archive.gz.age", "matrix-postgres": "synapse.sql.gz.age"}
        for datastore, artifact in expected.items():
            result = self.run_backup(datastore, "ok", "--age-recipients-file", str(self.recipients))
            self.assertEqual(result.returncode, 0, result.stderr)
            run = next(self.output.glob(f"{datastore}-*"))
            self.assertRegex(run.name, rf"^{datastore}-\d{{8}}T\d{{6}}Z-")
            self.assertEqual([p.name for p in run.iterdir()], [artifact])
            self.assertEqual(stat.S_IMODE((run / artifact).stat().st_mode), 0o600)
            blob = (run / artifact).read_bytes()
            self.assertTrue(blob.startswith(b"AGE-FIXTURE\n"))
            self.assertIn(b"synthetic dump", gzip.decompress(blob.removeprefix(b"AGE-FIXTURE\n")))
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)

    def test_credentials_stay_inside_the_pod(self):
        self.run_backup("matrix-postgres", "ok", "--age-recipients-file", str(self.recipients))
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        dump = next(c for c in calls if c[6] == "exec")
        self.assertIn("pod/matrix-postgres-0", dump)
        self.assertIn('PGPASSWORD="$POSTGRES_PASSWORD"', dump[-1])
        self.assertNotIn("secret", " ".join(dump).lower().replace("credential", ""))

    def test_mongodb_password_is_not_a_command_line_argument(self):
        self.run_backup("mongodb", "ok", "--age-recipients-file", str(self.recipients))
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        script = next(c for c in calls if c[6] == "exec")[-1]
        self.assertNotIn("--password", script)
        self.assertIn("umask 077", script)
        self.assertIn("--config=", script)
        self.assertIn("rm -f", script)

    def test_failures_publish_nothing(self):
        for scenario in ("missing-pod", "not-ready", "failed-dump", "empty-dump"):
            result = self.run_backup("mongodb", scenario, "--age-recipients-file", str(self.recipients))
            self.assertEqual(result.returncode, 1, scenario)
            self.assertEqual([n for n in self.published() if not n.startswith(".")], [], scenario)
            self.assertEqual(self.published(), [], scenario)

    def test_a_backup_requires_recipients_and_an_output_folder(self):
        self.assertEqual(self.run_backup("mongodb").returncode, 2)
        bad = Path(self.recipients.parent / "bad.txt")
        bad.write_text("not-a-recipient\n")
        result = self.run_backup("mongodb", "ok", "--age-recipients-file", str(bad))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.published(), [])
        self.assertEqual(self.run_backup("mongodb", "ok", "--check-target", with_output=False).returncode, 0)

    def test_target_check_reads_no_data(self):
        result = self.run_backup("matrix-postgres", "ok", "--check-target", with_output=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("matrix-postgres target ready", result.stdout)
        actions = [json.loads(line)[6] for line in self.calls.read_text().splitlines()]
        self.assertEqual(actions, ["wait", "get"])



class DatastoreBackupUnitsTest(unittest.TestCase):
    def test_units_check_the_target_then_back_up_into_a_private_folder(self):
        import configparser
        import shlex
        for datastore in ("mongodb", "matrix-postgres"):
            parser = configparser.ConfigParser(strict=False, interpolation=None)
            parser.optionxform = str
            parser.read(ROOT / "runbooks" / "systemd" / f"oriso-{datastore}-backup.service")
            service = parser["Service"]
            self.assertEqual(service["UMask"], "0077")
            pre, run = shlex.split(service["ExecStartPre"]), shlex.split(service["ExecStart"])
            self.assertEqual(pre[:3], ["/usr/bin/python3", "-I", "/opt/oriso/datastore-backup/backup_datastores.py"])
            self.assertEqual(pre[3:7], ["/etc/rancher/k3s/k3s.yaml", "default", "caritas", datastore])
            self.assertEqual(pre[7:], ["--check-target"])
            self.assertEqual(run[3:8], pre[3:7] + [f"/root/db-backups/{datastore}-helm"])
            self.assertEqual(run[8:], ["--age-recipients-file", "/etc/oriso/backup.age-recipients"])
            timer = configparser.ConfigParser(strict=False, interpolation=None)
            timer.optionxform = str
            timer.read(ROOT / "runbooks" / "systemd" / f"oriso-{datastore}-backup.timer")
            self.assertRegex(timer["Timer"]["OnCalendar"], r"^\*-\*-\* 02:\d\d:00 UTC$")
            self.assertEqual(timer["Timer"]["Persistent"], "true")

if __name__ == "__main__":
    unittest.main()
