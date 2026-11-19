"""Exercise the host backup command against a disposable kubectl fixture."""

import gzip
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup_mariadb.py"
KUBECTL_FIXTURE = """
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["BACKUP_TEST_CALLS"], "a") as calls:
    calls.write(json.dumps(args) + "\\n")

action = args[6]
scenario = os.environ["BACKUP_TEST_SCENARIO"]
if action == "wait":
    sys.exit(1 if scenario == "missing-pod" else 0)
if any("SHOW DATABASES" in arg for arg in args):
    if scenario == "discovery-failure":
        print("userservice")
        sys.exit(1)
    print("information_schema\\nmysql\\nperformance_schema\\nsys")
    if scenario != "no-app-schemas":
        print("tenantservice\\nuserservice")
    sys.exit(0)

schema = args[-1]
if scenario == "empty-dump":
    sys.exit(0)
print(f"-- synthetic dump of {schema}")
sys.exit(1 if scenario == "partial-dump" and schema == "userservice" else 0)
"""


class MariaDbBackupTest(unittest.TestCase):
    def run_backup(self, scenario):
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BACKUP_TEST_CALLS": str(self.calls),
            "BACKUP_TEST_SCENARIO": scenario,
        }
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.kubeconfig), "audit-dev",
             "caritas", str(self.output)],
            env=env, text=True, capture_output=True, timeout=15,
        )

    def setUp(self):
        scratch = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(scratch)
        self.output = root / "backups"
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        self.calls = root / "calls.jsonl"
        self.kubeconfig = root / "synthetic-kubeconfig"
        self.kubeconfig.touch()
        fake = self.bin_dir / "kubectl"
        fake.write_text(f"#!{sys.executable}\n" + KUBECTL_FIXTURE)
        fake.chmod(0o700)

    def calls_recorded(self):
        return [json.loads(row) for row in self.calls.read_text().splitlines()]

    def test_lookup_failure_never_creates_an_output_directory(self):
        result = self.run_backup("missing-pod")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(len(self.calls_recorded()), 1)

    def test_failed_or_empty_discovery_never_creates_an_output_directory(self):
        for scenario in ["discovery-failure", "no-app-schemas"]:
            with self.subTest(scenario=scenario):
                result = self.run_backup(scenario)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse(self.output.exists())

    def test_failed_dump_hides_partial_and_earlier_successful_files(self):
        self.output.mkdir(mode=0o700)
        old = self.output / "existing-backup"
        old.mkdir()
        for scenario in ["partial-dump", "empty-dump"]:
            with self.subTest(scenario=scenario):
                result = self.run_backup(scenario)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(list(self.output.iterdir()), [old])

    def test_two_complete_runs_are_private_and_pin_every_kubernetes_call(self):
        for _ in range(2):
            result = self.run_backup("success")
            self.assertEqual(result.returncode, 0, result.stderr)
        runs = list(self.output.iterdir())
        self.assertEqual(len(runs), 2)
        for directory in runs:
            self.assertTrue(directory.name.startswith("mariadb-"))
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(
                {file.name for file in directory.iterdir()},
                {"tenantservice.sql.gz", "userservice.sql.gz"},
            )
            for schema in ("tenantservice", "userservice"):
                file = directory / f"{schema}.sql.gz"
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
                with gzip.open(file, "rt") as restored:
                    self.assertEqual(restored.read(), f"-- synthetic dump of {schema}\n")
        calls = self.calls_recorded()
        self.assertEqual(len(calls), 8)
        for args in calls:
            self.assertEqual(
                args[:6],
                ["--kubeconfig", str(self.kubeconfig), "--context", "audit-dev",
                 "--namespace", "caritas"],
            )
            self.assertIn("pod/mariadb-0", args)
            if "--databases" in " ".join(args):
                self.assertEqual(args[6:10], ["exec", "pod/mariadb-0", "--container", "mariadb"])


if __name__ == "__main__":
    unittest.main()
