from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from dotenv import load_dotenv

load_dotenv()

CHART_REF_DEFAULT = (
    "https://github.com/kentik/kagent-helm/archive/refs/heads/main.tar.gz"
)
API_ROOT_DEFAULT = "grpc.api.kentik.com"


@dataclass
class InboundServicesConfig:
    service_type: str
    shared_lb_ip: str
    flow_enabled: bool
    flow_port: int
    snmp_trap_enabled: bool
    snmp_trap_port: int
    syslog_enabled: bool
    syslog_port: int


@dataclass
class KagentConfig:
    release_name: str
    namespace: str
    replica_count: int
    helm_chart_ref: str
    storage_class: str
    config_dir: Path
    company_id: str
    provisioning_token: str | None
    api_email: str | None
    api_token: str | None
    api_root: str
    token_name: str
    auto_approve: bool
    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str
    linux_capabilities: list[str]
    inbound_services: InboundServicesConfig


def load_config(config_path: str | Path = "kagent.yaml", replicas: int | None = None) -> KagentConfig:
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    release_name = raw["release_name"]

    svc_raw = raw.get("inbound_services", {})
    flow_raw = svc_raw.get("flow", {})
    trap_raw = svc_raw.get("snmp_trap", {})
    syslog_raw = svc_raw.get("syslog", {})
    inbound = InboundServicesConfig(
        service_type=svc_raw.get("type", "LoadBalancer"),
        shared_lb_ip=svc_raw.get("shared_lb_ip", ""),
        flow_enabled=flow_raw.get("enabled", False),
        flow_port=flow_raw.get("port", 9995),
        snmp_trap_enabled=trap_raw.get("enabled", False),
        snmp_trap_port=trap_raw.get("port", 162),
        syslog_enabled=syslog_raw.get("enabled", False),
        syslog_port=syslog_raw.get("port", 514),
    )

    return KagentConfig(
        release_name=release_name,
        namespace=raw["namespace"],
        replica_count=replicas if replicas is not None else raw.get("replica_count", 1),
        helm_chart_ref=raw.get("helm_chart_ref", CHART_REF_DEFAULT),
        storage_class=raw.get("storage_class", ""),
        config_dir=Path(os.getenv("KAGENT_CONFIG_DIR", "./config-kagent")),
        company_id=os.environ["KENTIK_COMPANY_ID"],
        # KB docs omit provisioningToken; treat as optional and auto-generate if absent
        provisioning_token=os.environ.get("KENTIK_PROVISIONING_TOKEN"),
        api_email=os.environ.get("K_API_EMAIL"),
        api_token=os.environ.get("K_API_TOKEN"),
        api_root=os.environ.get("K_API_ROOT", API_ROOT_DEFAULT),
        token_name=raw.get("token_name", "") or f"kagent-{release_name}",
        auto_approve=raw.get("auto_approve", False),
        cpu_request=raw.get("resources", {}).get("requests", {}).get("cpu", "250m"),
        cpu_limit=raw.get("resources", {}).get("limits", {}).get("cpu", "1"),
        memory_request=raw.get("resources", {}).get("requests", {}).get("memory", "256Mi"),
        memory_limit=raw.get("resources", {}).get("limits", {}).get("memory", "512Mi"),
        linux_capabilities=raw.get("linux_capabilities", ["NET_RAW"]),
        inbound_services=inbound,
    )


# --- Provisioning token ---

def generate_provisioning_token(cfg: KagentConfig) -> str:
    if not cfg.api_email or not cfg.api_token:
        print(
            "❌ K_API_EMAIL and K_API_TOKEN are required to generate a "
            "provisioning token.\n"
            "   Set them in .env, or set KENTIK_PROVISIONING_TOKEN directly.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Strip scheme and trailing slash; API root is always accessed over HTTPS
    api_root = cfg.api_root.removeprefix("https://").removeprefix("http://").rstrip("/")
    url = f"https://{api_root}/kagent/v202401/provisioning-tokens"
    body: dict = {
        "name": cfg.token_name,
        # "maxUsageCount": 1,
        "maxUsageCount": cfg.replica_count,
        "requiresApproval": not cfg.auto_approve,
    }

    print(f"  Calling Kentik API: {url}")
    print(f"  Token name: {cfg.token_name}  max_usage: {cfg.replica_count}  "
          f"requires_approval: {not cfg.auto_approve}")

    resp = requests.post(
        url,
        json=body,
        headers={
            "X-CH-Auth-Email": cfg.api_email,
            "X-CH-Auth-API-Token": cfg.api_token,
            "Content-Type": "application/json",
        },
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"  ❌ API returned HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)

    data = resp.json()
    token_value = data.get("token", {}).get("token")
    if not token_value:
        print(f"  ❌ No token in response: {resp.text}", file=sys.stderr)
        sys.exit(1)

    expires = data.get("token", {}).get("expiresAt", "N/A")
    print(f"  ✅ Provisioning token created (expires: {expires})")
    print("\n  Add to .env to reuse without regenerating:")
    print(f"  KENTIK_PROVISIONING_TOKEN={token_value}\n")
    return token_value


# --- Preflight ---

def check_prerequisites() -> None:
    missing = [tool for tool in ("helm", "kubectl") if shutil.which(tool) is None]
    if missing:
        print(f"❌ Missing required tools: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


LOCAL_PATH_PROVISIONER_URL = (
    "https://raw.githubusercontent.com/rancher/local-path-provisioner"
    "/v0.0.30/deploy/local-path-storage.yaml"
)


def _install_local_path_provisioner(cfg_dir: Path) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    manifest = cfg_dir / "local-path-storage.yaml"
    print("  Downloading local-path-provisioner manifest...")
    resp = requests.get(LOCAL_PATH_PROVISIONER_URL, timeout=30)
    resp.raise_for_status()
    manifest.write_text(resp.text)
    print(f"  Stored at {manifest}")

    subprocess.run(["kubectl", "apply", "-f", str(manifest)], check=True)
    _label_namespace_privileged("local-path-storage")

    print("  Waiting for local-path-provisioner to be ready...")
    subprocess.run(
        ["kubectl", "rollout", "status", "deployment/local-path-provisioner",
         "-n", "local-path-storage", "--timeout=120s"],
        check=True,
    )
    subprocess.run(
        ["kubectl", "patch", "storageclass", "local-path", "-p",
         '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'],
        check=True,
        capture_output=True,
    )
    print("  ✅ local-path-provisioner installed and set as default StorageClass.")


def check_storage_class(storage_class: str, cfg_dir: Path) -> None:
    if storage_class:
        result = subprocess.run(
            ["kubectl", "get", "storageclass", storage_class],
            capture_output=True, check=False,
        )
        if result.returncode != 0:
            print(f"❌ StorageClass '{storage_class}' not found.", file=sys.stderr)
            print("   Create it or leave storage_class empty to use the cluster default.", file=sys.stderr)
            sys.exit(1)
        return

    # No explicit class — check for a default; auto-install local-path if missing
    result = subprocess.run(
        ["kubectl", "get", "storageclass", "-o",
         "jsonpath={.items[?(@.metadata.annotations.storageclass\\.kubernetes\\.io/is-default-class==\"true\")].metadata.name}"],
        capture_output=True, text=True, check=False,
    )
    if not result.stdout.strip():
        print("  No default StorageClass found — installing local-path-provisioner...")
        _install_local_path_provisioner(cfg_dir)
    else:
        # Provisioner may already be installed; ensure its namespace is privileged
        _label_namespace_privileged("local-path-storage")


# --- Keypair generation ---

def _generate_keypair() -> tuple[str, str]:
    """Returns (private_pem, public_pem) for a new ed25519 keypair."""
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def _secret_exists(name: str, namespace: str) -> bool:
    result = subprocess.run(
        ["kubectl", "get", "secret", name, "-n", namespace],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def create_keypair_secrets(cfg: KagentConfig) -> None:
    key_dir = cfg.config_dir / "keys"
    key_dir.mkdir(parents=True, exist_ok=True)

    for i in range(cfg.replica_count):
        secret_name = f"{cfg.release_name}-{i}-secret"
        if _secret_exists(secret_name, cfg.namespace):
            print(f"  ⏭️  {secret_name} already exists, skipping.")
            continue

        print(f"  Generating keypair for replica {i}...")
        private_pem, public_pem = _generate_keypair()

        # Back up PEM files — store these securely and never commit unencrypted
        (key_dir / f"private_key_{i}.pem").write_text(private_pem)
        (key_dir / f"public_key_{i}.pem").write_text(public_pem)

        subprocess.run(
            [
                "kubectl", "create", "secret", "generic", secret_name,
                f"--from-literal=private_key.pem={private_pem}",
                f"--from-literal=public_key.pem={public_pem}",
                "-n", cfg.namespace,
            ],
            check=True,
            capture_output=True,
        )
        print(f"  ✅ {secret_name} created.")

    print("  ℹ️  Keypair backups written to config-kagent/keys/ — store these securely.")


# --- Namespace ---

def _label_namespace_privileged(namespace: str) -> None:
    subprocess.run(
        [
            "kubectl", "label", "namespace", namespace,
            "pod-security.kubernetes.io/enforce=privileged",
            "pod-security.kubernetes.io/warn=privileged",
            "pod-security.kubernetes.io/audit=privileged",
            "--overwrite",
        ],
        check=True,
        capture_output=True,
    )


def ensure_namespace(namespace: str) -> None:
    result = subprocess.run(
        ["kubectl", "get", "namespace", namespace],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        subprocess.run(
            ["kubectl", "create", "namespace", namespace],
            check=True,
            capture_output=True,
        )
        print(f"  ✅ Namespace '{namespace}' created.")

    # kagent requires NET_RAW; label namespace to allow privileged workloads
    _label_namespace_privileged(namespace)
    print(f"  ✅ Namespace '{namespace}' ready (PodSecurity: privileged).")


def ensure_local_path_storage() -> None:
    result = subprocess.run(
        ["kubectl", "get", "namespace", "local-path-storage"],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        return  # provisioner not installed; storage check handles that
    # local-path provisioner helper pod uses hostPath; must be privileged
    _label_namespace_privileged("local-path-storage")


# --- Helm install ---

def _release_exists(release_name: str, namespace: str) -> bool:
    result = subprocess.run(
        ["helm", "status", release_name, "--namespace", namespace],
        capture_output=True, check=False,
    )
    return result.returncode == 0


# --- MetalLB / LoadBalancer check ---

def check_load_balancer(service_type: str) -> None:
    if service_type != "LoadBalancer":
        return
    result = subprocess.run(
        ["kubectl", "get", "namespace", "metallb-system"],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        print("  ⚠️  MetalLB not detected (metallb-system namespace missing).", file=sys.stderr)
        print("     Install MetalLB before inbound Services will get an external IP:", file=sys.stderr)
        print("     https://metallb.universe.tf/installation/", file=sys.stderr)
        print("     Or change inbound_services.type to NodePort in kagent.yaml.", file=sys.stderr)


# --- Inbound Services ---

def _service_manifest(name: str, namespace: str, selector_name: str,
                       service_type: str, ports: list[dict],
                       shared_lb_ip: str = "") -> dict:
    annotations: dict = {}
    if service_type == "LoadBalancer" and shared_lb_ip:
        # Share one MetalLB IP across all kagent services (different ports, no conflict)
        annotations["metallb.universe.io/allow-shared-ip"] = "kagent-inbound"
        annotations["metallb.universe.io/loadBalancerIPs"] = shared_lb_ip
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": "deploy-kagent"},
            "annotations": annotations,
        },
        "spec": {
            "type": service_type,
            "selector": {"app.kubernetes.io/name": selector_name},
            "ports": ports,
        },
    }


def create_inbound_services(cfg: KagentConfig) -> None:
    svc = cfg.inbound_services
    ports: list[dict] = []

    if svc.flow_enabled:
        ports.append({"name": "flow", "protocol": "UDP",
                      "port": svc.flow_port, "targetPort": svc.flow_port})
    if svc.snmp_trap_enabled:
        ports.append({"name": "snmptrap", "protocol": "UDP",
                      "port": svc.snmp_trap_port, "targetPort": svc.snmp_trap_port})
    if svc.syslog_enabled:
        ports += [
            {"name": "syslog-udp", "protocol": "UDP",
             "port": svc.syslog_port, "targetPort": svc.syslog_port},
            {"name": "syslog-tcp", "protocol": "TCP",
             "port": svc.syslog_port, "targetPort": svc.syslog_port},
        ]

    if not ports:
        return

    # Single Service with all inbound ports — avoids MetalLB IP-sharing complexity
    annotations: dict = {}
    if svc.service_type == "LoadBalancer" and svc.shared_lb_ip:
        annotations["metallb.universe.io/loadBalancerIPs"] = svc.shared_lb_ip

    manifest = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{cfg.release_name}-inbound",
            "namespace": cfg.namespace,
            "labels": {"app.kubernetes.io/managed-by": "deploy-kagent"},
            "annotations": annotations,
        },
        "spec": {
            "type": svc.service_type,
            "externalTrafficPolicy": "Local",
            "selector": {"app.kubernetes.io/name": cfg.release_name},
            "ports": ports,
        },
    }

    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=yaml.dump(manifest, default_flow_style=False),
        text=True, capture_output=True, check=True,
    )
    print(f"  ✅ Service '{cfg.release_name}-inbound' applied ({svc.service_type}).")
    print("     Ports: " + ", ".join(f"{p['port']}/{p['protocol']}" for p in ports))


def wait_for_service_ips(cfg: KagentConfig, timeout: int = 120) -> None:
    svc = cfg.inbound_services
    if svc.service_type != "LoadBalancer":
        return

    svc_name = f"{cfg.release_name}-inbound"
    print(f"  Waiting for LoadBalancer IP on '{svc_name}'...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["kubectl", "get", "svc", svc_name, "-n", cfg.namespace,
             "-o", "jsonpath={.status.loadBalancer.ingress[0].ip}"],
            capture_output=True, text=True, check=False,
        )
        if result.stdout.strip():
            break
        time.sleep(5)

    result = subprocess.run(
        ["kubectl", "get", "svc", svc_name, "-n", cfg.namespace,
         "-o", "jsonpath={.status.loadBalancer.ingress[0].ip}"],
        capture_output=True, text=True, check=False,
    )
    ip = result.stdout.strip() or "(pending)"
    print(f"\n  All inbound traffic → {ip}")
    print("  Configure network devices to send:")
    if svc.flow_enabled:
        print(f"    Flow     → {ip}:{svc.flow_port} (UDP — NetFlow/sFlow/IPFIX)")
    if svc.snmp_trap_enabled:
        print(f"    SNMP trap → {ip}:{svc.snmp_trap_port}")
    if svc.syslog_enabled:
        print(f"    Syslog   → {ip}:{svc.syslog_port} (UDP+TCP)")


def helm_install(cfg: KagentConfig) -> bool:
    """Deploy or upgrade the Helm release. Returns True if this was an upgrade."""
    is_upgrade = _release_exists(cfg.release_name, cfg.namespace)
    cmd = [
        "helm", "upgrade", "--install", cfg.release_name,
        cfg.helm_chart_ref,
        "--namespace", cfg.namespace,
        "--set", "deploymentType=statefulset",
        "--set", f"replicaCount={cfg.replica_count}",
        "--set", "persistence.keypair.type=secret",
        # allowPrivilegeEscalation=true required for non-root process to hold capabilities in CapEff
        "--set", "securityContext.allowPrivilegeEscalation=true",
        # runAsUser=0 required: ambient caps are cleared on setresuid root→non-root
        "--set", "podSecurityContext.runAsNonRoot=false",
        "--set", "podSecurityContext.runAsUser=0",
        # hostNetwork=true: pod shares host network namespace so source IPs are never masqueraded by CNI
        "--set", "hostNetwork=true",
        "--set-string", f"kagent.companyId={cfg.company_id}",
        "--set", f"resources.requests.cpu={cfg.cpu_request}",
        "--set", f"resources.requests.memory={cfg.memory_request}",
        "--set", f"resources.limits.cpu={cfg.cpu_limit}",
        "--set", f"resources.limits.memory={cfg.memory_limit}",
    ]
    # Build capabilities set from kagent.yaml linux_capabilities list
    caps = ",".join(cfg.linux_capabilities)
    cmd += ["--set", f"securityContext.capabilities.add={{{caps}}}"]
    if cfg.provisioning_token:
        cmd += ["--set-string", f"kagent.provisioningToken={cfg.provisioning_token}"]
    if cfg.storage_class:
        cmd += ["--set", f"persistence.pvc.storageClass={cfg.storage_class}"]

    action = "Upgrading" if is_upgrade else "Installing"
    print(f"  {action} release '{cfg.release_name}'...")
    subprocess.run(cmd, check=True)
    print("  ✅ Helm release applied.")
    return is_upgrade


def rollout_restart(cfg: KagentConfig) -> None:
    print(f"  Rolling restart of statefulset/{cfg.release_name}...")
    subprocess.run(
        ["kubectl", "rollout", "restart", f"statefulset/{cfg.release_name}",
         "-n", cfg.namespace],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["kubectl", "rollout", "status", f"statefulset/{cfg.release_name}",
         "-n", cfg.namespace, "--timeout=120s"],
        check=True,
    )
    print("  ✅ Rollout complete.")


# --- Verify ---

def wait_for_pods(cfg: KagentConfig, timeout: int = 600) -> None:
    expected = [f"{cfg.release_name}-{i}" for i in range(cfg.replica_count)]
    print(f"  Waiting for pods: {', '.join(expected)}...")
    deadline = time.monotonic() + timeout
    delay = 5.0
    last_status: list[str] = []

    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "kubectl", "get", "pods",
                "-l", f"app.kubernetes.io/name={cfg.release_name}",
                "-n", cfg.namespace,
                "--no-headers",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        lines = [line for line in result.stdout.strip().splitlines() if line]
        # Print status only when it changes so the output isn't noisy
        if lines != last_status:
            for line in lines:
                print(f"    {line}")
            last_status = lines
        running = [line for line in lines if "Running" in line]
        if len(running) >= cfg.replica_count:
            print(f"  ✅ All {cfg.replica_count} pod(s) Running.")
            return
        time.sleep(delay)
        delay = min(delay * 1.5, 30)

    # Print final state to help diagnose what's stuck
    print("\n  Current pod state:", file=sys.stderr)
    for line in last_status:
        print(f"    {line}", file=sys.stderr)
    print(file=sys.stderr)
    raise TimeoutError(
        f"Pods did not reach Running state within {timeout}s.\n"
        f"  kubectl describe pod {cfg.release_name}-0 -n {cfg.namespace}"
    )


# --- Dry run ---

def dry_run(cfg: KagentConfig) -> None:
    svc = cfg.inbound_services
    print(f"=== DRY RUN: {cfg.release_name} ===\n")
    print(f"Namespace    : {cfg.namespace}")
    print(f"Release      : {cfg.release_name}")
    print(f"Replicas     : {cfg.replica_count}")
    print(f"Chart        : {cfg.helm_chart_ref}")
    print(f"Storage class: {cfg.storage_class or '(cluster default)'}")
    print(f"Company ID   : {cfg.company_id}")
    if cfg.provisioning_token:
        print("Prov. token  : set (from KENTIK_PROVISIONING_TOKEN)")
    elif cfg.api_email and cfg.api_token:
        print(f"Prov. token  : will be generated via API (name: {cfg.token_name}, "
              f"max_usage: {cfg.replica_count}, auto_approve: {cfg.auto_approve})")
    else:
        print("Prov. token  : not set — set KENTIK_PROVISIONING_TOKEN or provide "
              "K_API_EMAIL + K_API_TOKEN to auto-generate")
    print(f"Capabilities : {', '.join(cfg.linux_capabilities)}")
    print()
    print("Inbound Services:")
    if svc.flow_enabled:
        print(f"  kagent-inbound ({svc.service_type})  UDP {svc.flow_port} (flow: NetFlow/sFlow/IPFIX)")
    if svc.snmp_trap_enabled:
        print(f"  kagent-snmptrap ({svc.service_type})  UDP {svc.snmp_trap_port} (SNMP traps)")
    if svc.syslog_enabled:
        print(f"  kagent-syslog   ({svc.service_type})  UDP+TCP {svc.syslog_port} (Syslog)")
    if not any([svc.flow_enabled, svc.snmp_trap_enabled, svc.syslog_enabled]):
        print("  (none configured)")
    print()
    print("Secrets that would be created (skipped if already exist):")
    for i in range(cfg.replica_count):
        print(f"  {cfg.release_name}-{i}-secret")
    print("\nPods expected after install:")
    for i in range(cfg.replica_count):
        print(f"  {cfg.release_name}-{i}")
    print("\nSteps:")
    steps = [
        "0. Generate provisioning token via Kentik API (skipped if KENTIK_PROVISIONING_TOKEN is set)",
        "1. Ensure namespace exists (with PodSecurity: privileged)",
        "2. Generate ed25519 keypairs and create k8s secrets",
        "3. helm upgrade --install (StatefulSet, capabilities: " + ", ".join(cfg.linux_capabilities) + ")",
        "4. Wait for all pods to reach Running state",
        "5. Create/update inbound Services for flow, SNMP traps, syslog",
    ]
    for s in steps:
        print(f"  {s}")


# --- Main ---

def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy kagent via Helm")
    parser.add_argument("--config", default="kagent.yaml", help="Path to kagent.yaml")
    parser.add_argument("--replicas", type=int, default=None, help="Number of replicas (overrides kagent.yaml)")
    parser.add_argument("--token-name", default=None, help="Name for the provisioning token (overrides kagent.yaml)")
    parser.add_argument("--auto-approve", action="store_true", help="Create token without requiring manual Portal approval")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without executing")
    args = parser.parse_args()

    cfg = load_config(args.config, replicas=args.replicas)
    if args.token_name:
        cfg.token_name = args.token_name
    if args.auto_approve:
        cfg.auto_approve = True

    if args.dry_run:
        dry_run(cfg)
        return

    # Preflight runs after dry-run check so the plan is always printable
    check_prerequisites()
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    check_storage_class(cfg.storage_class, cfg.config_dir)
    check_load_balancer(cfg.inbound_services.service_type)

    if not cfg.provisioning_token:
        print("=== Step 0: Generating provisioning token ===")
        cfg.provisioning_token = generate_provisioning_token(cfg)

    print("=== Step 1: Namespace ===")
    ensure_namespace(cfg.namespace)

    print("\n=== Step 2: Keypair secrets ===")
    create_keypair_secrets(cfg)

    print("\n=== Step 3: Helm install ===")
    try:
        is_upgrade = helm_install(cfg)
    except subprocess.CalledProcessError as e:
        print(f"  ❌ Helm failed: {e}", file=sys.stderr)
        sys.exit(1)

    if is_upgrade:
        print("\n=== Step 3b: Rolling restart (upgrade detected) ===")
        try:
            rollout_restart(cfg)
        except subprocess.CalledProcessError as e:
            print(f"  ❌ Rollout restart failed: {e}", file=sys.stderr)
            sys.exit(1)

    print("\n=== Step 4: Waiting for pods ===")
    try:
        wait_for_pods(cfg)
    except TimeoutError as e:
        print(f"  ❌ {e}", file=sys.stderr)
        sys.exit(1)

    print("\n=== Step 5: Creating inbound Services ===")
    try:
        create_inbound_services(cfg)
        wait_for_service_ips(cfg)
    except subprocess.CalledProcessError as e:
        print(f"  ❌ Service creation failed: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n✅ kagent deployed successfully.")
    print("\nNext: authorize the agent in the Kentik Portal:")
    print("  Settings → Universal Agents → Pending Authorization")
    print("\nVerify connectivity from inside the pod:")
    print(
        f"  kubectl exec -it {cfg.release_name}-0 -n {cfg.namespace} -- "
        f"bash -c \"timeout 1 bash -c '</dev/tcp/grpc.api.kentik.com/443' "
        f"&& echo open || echo failed\""
    )


if __name__ == "__main__":
    main()
