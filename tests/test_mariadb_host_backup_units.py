"""Keep the host MariaDB backup units aligned with the script and the rendered StatefulSet."""

import configparser
from pathlib import Path
import re
import shlex
import unittest

from render_signoz_dev_observability_test import by_key, render


ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "runbooks" / "systemd"
SCRIPT = ROOT / "scripts" / "backup_mariadb.py"
INSTALLED_SCRIPT = "/opt/oriso/mariadb-backup/backup_mariadb.py"


def unit(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(UNITS / name)
    return parser


class MariaDbHostBackupUnitsTest(unittest.TestCase):
    def test_script_pod_matches_the_rendered_dev_statefulset(self):
        # The legacy host job pinned "oriso-mariadb-0" and failed every night
        # once the chart named the StatefulSet "mariadb".
        pod = re.search(r'^POD = "pod/([^"]+)"$', SCRIPT.read_text(), re.M)[1]
        statefulset = by_key(render("values-dev.yaml"))[("StatefulSet", "mariadb")]
        self.assertEqual(pod, statefulset["metadata"]["name"] + "-0")

    def test_service_checks_the_target_before_the_backup(self):
        service = unit("oriso-mariadb-backup.service")["Service"]
        self.assertEqual(service["Type"], "oneshot")
        self.assertEqual(service["UMask"], "0077")
        pre, run = shlex.split(service["ExecStartPre"]), shlex.split(service["ExecStart"])
        for command in (pre, run):
            self.assertEqual(command[:3], ["/usr/bin/python3", "-I", INSTALLED_SCRIPT])
            self.assertEqual(command[3:6], ["/etc/rancher/k3s/k3s.yaml", "default", "caritas"])
        self.assertEqual(pre[6:], ["--check-target"])
        self.assertEqual(len(run), 7)
        self.assertTrue(run[6].startswith("/root/db-backups/"))

    def test_timer_runs_nightly_and_catches_up_after_downtime(self):
        timer = unit("oriso-mariadb-backup.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "*-*-* 02:00:00 UTC")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")


if __name__ == "__main__":
    unittest.main()
