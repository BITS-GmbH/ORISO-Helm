"""The Keycloak startup gate renders on the private management interface."""

import unittest

from render_signoz_dev_observability_test import by_key, env, render


SUBCHART = "online-counseling-keycloak"
PROBE_VALUES = f"{SUBCHART}.keycloakProbes"
DEPLOYMENT = ("Deployment", "keycloak")
SERVICE = ("Service", "keycloak")
PROBES = {"startupProbe": "started", "livenessProbe": "live", "readinessProbe": "ready"}


def resources(*overlays, args=()):
    return by_key(render(*overlays, helm_args=args))


def container(chart):
    return chart[DEPLOYMENT]["spec"]["template"]["spec"]["containers"][0]


class KeycloakReadinessTest(unittest.TestCase):
    def test_dev_health_and_database_checks_gate_traffic_without_routing_management(self):
        dev = resources("values-dev.yaml")
        off = resources("values-dev.yaml", args=("--set", f"{PROBE_VALUES}.useManagementPort=false"))
        keycloak = container(dev)
        self.assertIn({"name": "management", "containerPort": 9000}, keycloak["ports"])
        settings = env(dev[DEPLOYMENT])
        self.assertEqual(settings["KC_HEALTH_ENABLED"], "true")
        self.assertEqual(settings["KC_METRICS_ENABLED"], "true")
        for probe, path in PROBES.items():
            self.assertEqual(keycloak[probe]["httpGet"], {
                "path": f"/auth/health/{path}", "port": "management",
            })
        self.assertEqual(dev[SERVICE], off[SERVICE])
        self.assertEqual(
            {key: doc for key, doc in dev.items() if key[0] == "Ingress"},
            {key: doc for key, doc in off.items() if key[0] == "Ingress"},
        )

    def test_management_health_works_with_metrics_scraping_off(self):
        chart = resources("values-dev.yaml", args=("--set", "global.observability.appMetrics=false"))
        keycloak = container(chart)
        self.assertIn({"name": "management", "containerPort": 9000}, keycloak["ports"])
        settings = env(chart[DEPLOYMENT])
        self.assertEqual(settings["KC_HEALTH_ENABLED"], "true")
        self.assertEqual(settings["KC_METRICS_ENABLED"], "true")
        self.assertNotIn("KC_EVENT_METRICS_USER_ENABLED", settings)
        annotations = chart[DEPLOYMENT]["spec"]["template"]["metadata"]["annotations"]
        self.assertFalse(any(key.startswith("prometheus.io/") for key in annotations))

    def test_base_and_layered_pre_dev_keep_the_old_default(self):
        for overlays in ((), ("values-dev.yaml", "values-pre-dev.yaml")):
            with self.subTest(overlays=overlays):
                chart = resources(*overlays)
                self.assertNotIn("KC_HEALTH_ENABLED", env(chart[DEPLOYMENT]))
                self.assertTrue(PROBES.keys().isdisjoint(container(chart)))

    def test_existing_app_port_path_and_timing_overrides_still_render(self):
        chart = resources(args=(
            "--set-string", f"{PROBE_VALUES}.startup.path=/custom/started",
            "--set-string", f"{PROBE_VALUES}.liveness.path=/custom/live",
            "--set-string", f"{PROBE_VALUES}.readiness.path=/custom/ready",
            "--set", f"{PROBE_VALUES}.readiness.periodSeconds=7",
        ))
        keycloak = container(chart)
        self.assertNotIn("KC_HEALTH_ENABLED", env(chart[DEPLOYMENT]))
        self.assertEqual(keycloak["readinessProbe"]["periodSeconds"], 7)
        for probe, path in PROBES.items():
            self.assertEqual(keycloak[probe]["httpGet"], {"path": f"/custom/{path}", "port": 8080})


if __name__ == "__main__":
    unittest.main()
