#!/usr/bin/env python3
"""Render guard for the bundled ingress controller's availability (ORISO-Helm#544).

The chart's ingress-nginx controller runs two replicas by default, spread over
nodes by soft anti-affinity, with a PodDisruptionBudget that lets a node drain
proceed. A readiness probe keeps a new replica out of the Service until nginx
has loaded its backends, and a shutdown grace period keeps a terminating one
serving while traffic moves away. ip-anonymizer must stay at one replica: each
pod keeps its sessions in memory, so the render fails above one.
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROLLER = {
    "app.kubernetes.io/name": "ingress-nginx",
    "app.kubernetes.io/component": "controller",
    "app.kubernetes.io/instance": "ingress-nginx",
}


def helm(*overrides: str) -> subprocess.CompletedProcess:
    command = ["helm", "template", "caritas", CHART_DIR]
    for path in ("values.yaml.default", "tests/fixtures/values-render-domain.yaml",
                 "secrets.yaml.default", "tests/fixtures/render-required-secrets.yaml"):
        command += ["-f", os.path.join(CHART_DIR, path)]
    for override in overrides:
        command += ["--set", override]
    return subprocess.run(command, capture_output=True, text=True)


def render(*overrides: str) -> dict:
    result = helm(*overrides)
    assert result.returncode == 0, result.stderr
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(result.stdout) if d}


def main() -> None:
    docs = render()
    controller = docs[("Deployment", "ingress-nginx-controller")]
    assert controller["spec"]["replicas"] == 2, controller["spec"]["replicas"]
    # Default RollingUpdate (25%) gives maxUnavailable 0 / maxSurge 1 at two replicas: no gap on rollout.
    assert "strategy" not in controller["spec"], controller["spec"]["strategy"]

    [container] = controller["spec"]["template"]["spec"]["containers"]
    # Without readiness a new replica takes traffic before its backends load and answers 503.
    for probe in ("readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"] == {"path": "/healthz", "port": 10254}, container.get(probe)
    assert "--shutdown-grace-period=10" in container["args"], container["args"]

    affinity = controller["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"]
    assert "requiredDuringSchedulingIgnoredDuringExecution" not in affinity, "hard anti-affinity breaks single-node installs"
    [term] = affinity["preferredDuringSchedulingIgnoredDuringExecution"]
    assert term["podAffinityTerm"]["topologyKey"] == "kubernetes.io/hostname"
    assert term["podAffinityTerm"]["labelSelector"]["matchLabels"] == CONTROLLER
    assert controller["spec"]["template"]["metadata"]["labels"] == CONTROLLER

    pdb = docs[("PodDisruptionBudget", "ingress-nginx-controller")]
    assert pdb["spec"] == {"maxUnavailable": 1, "selector": {"matchLabels": CONTROLLER}}, pdb["spec"]

    # An operator can still choose one replica; the PDB then does not block a drain.
    single = render("nginxController.replicas=1")
    assert single[("Deployment", "ingress-nginx-controller")]["spec"]["replicas"] == 1
    assert single[("PodDisruptionBudget", "ingress-nginx-controller")]["spec"]["maxUnavailable"] == 1

    disabled = render("nginxController.enabled=false")
    assert ("PodDisruptionBudget", "ingress-nginx-controller") not in disabled
    assert ("Deployment", "ingress-nginx-controller") not in disabled

    anonymizer = render("ipAnonymizer.enabled=true")[("Deployment", "ip-anonymizer")]
    assert anonymizer["spec"]["replicas"] == 1
    [legacy_container] = anonymizer["spec"]["template"]["spec"]["containers"]
    assert not any(var["name"] == "ANON_SESSION_KEY" for var in legacy_container.get("env", []))

    keyed = render(
        "ipAnonymizer.enabled=true",
        "ipAnonymizer.sessionKeySecretName=ip-anonymizer-session-key",
    )[("Deployment", "ip-anonymizer")]
    [keyed_container] = keyed["spec"]["template"]["spec"]["containers"]
    assert [var for var in keyed_container.get("env", []) if var["name"] == "ANON_SESSION_KEY"] == [{
        "name": "ANON_SESSION_KEY",
        "valueFrom": {"secretKeyRef": {"name": "ip-anonymizer-session-key", "key": "ANON_SESSION_KEY"}},
    }], "expected ANON_SESSION_KEY from the configured Secret"
    blocked_scale = helm(
        "ipAnonymizer.enabled=true",
        "ipAnonymizer.sessionKeySecretName=ip-anonymizer-session-key",
        "ipAnonymizer.replicas=2",
    )
    assert blocked_scale.returncode != 0 and "ipAnonymizer.replicas must be 1" in blocked_scale.stderr
    scaled = helm("ipAnonymizer.enabled=true", "ipAnonymizer.replicas=2")
    assert scaled.returncode != 0 and "ipAnonymizer.replicas must be 1" in scaled.stderr, scaled.stderr
    # The guard only applies when ip-anonymizer is rendered.
    render("ipAnonymizer.replicas=2")

    print("PASS: ingress controller renders 2 spread, probed replicas with a drain-safe PDB; ip-anonymizer stays at 1")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
