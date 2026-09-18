"""Contract tests for the deployment configuration.

These lock in behaviour that is invisible until a cluster is running. Each
test corresponds to a defect that reached production at least once.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

# The loaders read credentials from the environment, and python-dotenv pulls in
# a local .env when one exists. Pin dummies first so the suite behaves the same
# on a developer machine and on a runner with no .env.
for _var in (
    "KENTIK_COMPANY_ID",
    "PROXMOX_USER",
    "PROXMOX_TOKEN_NAME",
    "PROXMOX_TOKEN_SECRET",
):
    os.environ[_var] = "test"

from deploy_talos_proxmox import deploy_kagent as dk
from deploy_talos_proxmox.config import load_config as load_talos_config

REPO = Path(__file__).resolve().parent.parent
KAGENT_EXAMPLE = REPO / "kagent.yaml.example"
TALOS_EXAMPLE = REPO / "talos.yaml.example"

# scamper is privilege-separated: it chroots to /var/empty, then setuid/setgid's
# the probe half. Without these, every ping fails with a misleading
# "could not open icmp4 socket" that points at NET_RAW instead.
PRIVSEP_CAPABILITIES = {"SYS_CHROOT", "SETUID", "SETGID"}


# --- Config contract ---------------------------------------------------------

def test_kagent_example_loads() -> None:
    cfg = dk.load_config(KAGENT_EXAMPLE)
    assert cfg.release_name
    assert cfg.namespace


def test_talos_example_loads() -> None:
    cfg = load_talos_config(TALOS_EXAMPLE)
    assert cfg.control_plane_nodes, "example must define a control-plane node"


def test_kagent_example_grants_privsep_capabilities() -> None:
    cfg = dk.load_config(KAGENT_EXAMPLE)
    missing = PRIVSEP_CAPABILITIES - set(cfg.linux_capabilities)
    assert not missing, f"synthetics break without {sorted(missing)}"


def test_kagent_default_capabilities_cover_privsep() -> None:
    """The fallback used when the key is absent must be safe too."""
    cfg = dk.load_config(KAGENT_EXAMPLE)
    with mock.patch.object(yaml, "safe_load", return_value={
        "release_name": cfg.release_name, "namespace": cfg.namespace,
    }):
        fallback = dk.load_config(KAGENT_EXAMPLE)
    assert PRIVSEP_CAPABILITIES <= set(fallback.linux_capabilities)


def test_worker_nodes_can_back_the_agent_memory_limit() -> None:
    """A memory limit is not scheduling-constrained; only the request is."""
    kagent = dk.load_config(KAGENT_EXAMPLE)
    talos = load_talos_config(TALOS_EXAMPLE)
    limit_mib = int(kagent.memory_limit.removesuffix("Mi"))
    workers = [n for n in talos.nodes if n.role == "worker"]
    assert workers, "example must define workers"
    for node in workers:
        assert node.memory > limit_mib, (
            f"{node.name} has {node.memory}MB but the agent may use "
            f"{limit_mib}Mi; the container can outgrow the node"
        )


# --- Rendered manifest contract ----------------------------------------------

@pytest.fixture(scope="session")
def rendered_statefulset() -> dict:
    """Render the chart with the exact flags `deploy-kagent` passes."""
    if shutil.which("helm") is None:
        pytest.skip("helm not installed")

    cfg = dk.load_config(KAGENT_EXAMPLE)
    cfg.provisioning_token = "dummy"
    captured: dict = {}

    def capture(cmd, *_a, **_k):
        captured["cmd"] = cmd
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch.object(dk.subprocess, "run", capture), \
         mock.patch.object(dk, "_release_exists", lambda *_a: False):
        dk.helm_install(cfg)

    cmd = captured["cmd"]
    flags = [
        part
        for i, arg in enumerate(cmd)
        if arg in ("--set", "--set-string")
        for part in (arg, cmd[i + 1])
    ]
    result = subprocess.run(
        ["helm", "template", cfg.release_name, cfg.helm_chart_ref, *flags],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"helm template unavailable: {result.stderr[:200]}")

    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    sets = [d for d in docs if d.get("kind") == "StatefulSet"]
    assert sets, "chart rendered no StatefulSet"
    return sets[0]


def test_rendered_container_has_privsep_capabilities(rendered_statefulset) -> None:
    ctr = rendered_statefulset["spec"]["template"]["spec"]["containers"][0]
    added = set(ctr["securityContext"]["capabilities"]["add"])
    assert PRIVSEP_CAPABILITIES <= added
    assert "NET_RAW" in added


def test_rendered_pod_runs_as_root(rendered_statefulset) -> None:
    """A non-root process receives no capabilities: they land in CapBnd only."""
    pod = rendered_statefulset["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == 0


def test_rendered_container_does_not_allow_privilege_escalation(
    rendered_statefulset,
) -> None:
    ctr = rendered_statefulset["spec"]["template"]["spec"]["containers"][0]
    assert ctr["securityContext"]["allowPrivilegeEscalation"] is False


def test_rendered_memory_limit_matches_config(rendered_statefulset) -> None:
    ctr = rendered_statefulset["spec"]["template"]["spec"]["containers"][0]
    expected = dk.load_config(KAGENT_EXAMPLE).memory_limit
    assert ctr["resources"]["limits"]["memory"] == expected


# --- Upstream chart canaries -------------------------------------------------

def test_chart_still_ignores_hostnetwork(rendered_statefulset) -> None:
    """`--set hostNetwork=true` is silently discarded by this chart.

    `deploy-kagent` patches the StatefulSet instead. If this fails, upstream
    added support and the patch can go.
    """
    pod = rendered_statefulset["spec"]["template"]["spec"]
    assert "hostNetwork" not in pod


def test_chart_keypair_init_still_reads_hostname(rendered_statefulset) -> None:
    """Under hostNetwork, $HOSTNAME is the node name, so the ordinal is empty.

    `deploy-kagent` injects HOSTNAME from the downward API. If this fails,
    upstream changed the script and the override needs rechecking.
    """
    pod = rendered_statefulset["spec"]["template"]["spec"]
    init = [c for c in pod.get("initContainers", []) if c["name"] == "setup-keypair"]
    assert init, "chart no longer defines setup-keypair"
    assert "$HOSTNAME" in init[0]["command"][-1]
    assert "env" not in init[0], "chart now sets env; the patch may conflict"


def test_talos_dry_run_needs_no_external_tools() -> None:
    """A plan touches nothing, so it must not require talosctl to be installed."""
    from deploy_talos_proxmox import deploy_talos as dt

    argv = ["deploy-talos", "--config", str(TALOS_EXAMPLE), "--dry-run"]
    with mock.patch.object(dt.shutil, "which", return_value=None), \
         mock.patch.object(sys, "argv", argv):
        dt.main()


def test_kagent_dry_run_needs_no_external_tools() -> None:
    argv = ["deploy-kagent", "--config", str(KAGENT_EXAMPLE), "--dry-run"]
    with mock.patch.object(dk.shutil, "which", return_value=None), \
         mock.patch.object(sys, "argv", argv):
        dk.main()


# --- Repository hygiene ------------------------------------------------------

def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO,
        capture_output=True, text=True, check=True,
    )
    return [REPO / p for p in out.stdout.split("\0") if p]


# Written as an escape so this file does not trip its own check.
EM_DASH = "\u2014"

TEXT_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".sh", ".toml", ".example"}


def test_no_em_dashes_in_tracked_files() -> None:
    offenders = [
        p.relative_to(REPO)
        for p in _tracked_files()
        if p.suffix in TEXT_SUFFIXES
        and p.is_file()
        and EM_DASH in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offenders, f"em dashes found in {offenders}"


def test_generated_secret_directories_are_ignored() -> None:
    """talosconfig holds an os:admin cert; config-kagent/keys holds private keys."""
    for path in ("config-talos/", "config-kagent/"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=REPO, check=False,
        )
        assert result.returncode == 0, f"{path} is not gitignored"


def test_no_secret_material_is_tracked() -> None:
    patterns = ("talosconfig", "private_key", ".pem")
    tracked = [str(p.relative_to(REPO)) for p in _tracked_files()]
    leaked = [f for f in tracked if any(pat in f for pat in patterns)]
    assert not leaked, f"secret material tracked: {leaked}"
