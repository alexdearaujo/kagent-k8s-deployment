from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import urllib3
import yaml
from proxmoxer import ProxmoxAPI

from .config import ClusterConfig, NetworkConfig, NodeConfig, ProxmoxConfig, load_config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# --- Preflight ---

def check_talosctl() -> None:
    if shutil.which("talosctl") is None:
        print("❌ talosctl not found. Install it with:", file=sys.stderr)
        print("   brew install siderolabs/tap/talosctl", file=sys.stderr)
        sys.exit(1)


# --- Proxmox ---

def connect_proxmox(cfg: ProxmoxConfig) -> ProxmoxAPI:
    print(f"Connecting to Proxmox at {cfg.host}...")
    api = ProxmoxAPI(
        cfg.host,
        user=cfg.user,
        token_name=cfg.token_name,
        token_value=cfg.token_value,
        verify_ssl=False,
    )
    print("Connected.\n")
    return api


def create_vm(proxmox: ProxmoxAPI, node: NodeConfig, cfg: ProxmoxConfig) -> None:
    print(f"  Creating VM {node.vmid} ({node.name}, {node.role})...")
    proxmox.nodes(cfg.node).qemu.post(
        vmid=node.vmid,
        name=node.name,
        memory=node.memory,
        cores=node.cores,
        cpu="host",
        scsihw="virtio-scsi-pci",
        scsi0=f"{cfg.storage_pool}:{node.disk_gb}",
        net0=f"virtio,bridge={cfg.bridge},tag={cfg.vlan_tag}",
        ide2=f"{cfg.iso_path},media=cdrom",
        boot="order=scsi0;ide2",
        agent="1",
        ostype="l26",
        onboot=0,
    )
    print(f"  ✅ {node.name} created.")


def get_existing_vmids(proxmox: ProxmoxAPI, proxmox_node: str) -> set[int]:
    return {vm["vmid"] for vm in proxmox.nodes(proxmox_node).qemu.get()}  # type: ignore[union-attr]


def delete_vm(proxmox: ProxmoxAPI, node: NodeConfig, proxmox_node: str) -> None:
    print(f"  Deleting {node.name} ({node.vmid})...")
    status = proxmox.nodes(proxmox_node).qemu(node.vmid).status.current.get()  # type: ignore[union-attr]
    if status["status"] == "running":  # type: ignore[index]
        proxmox.nodes(proxmox_node).qemu(node.vmid).status.stop.post()  # type: ignore[union-attr]
        for _ in range(30):
            time.sleep(2)
            current = proxmox.nodes(proxmox_node).qemu(node.vmid).status.current.get()  # type: ignore[union-attr]
            if current["status"] == "stopped":  # type: ignore[index]
                break
    proxmox.nodes(proxmox_node).qemu(node.vmid).delete()
    print(f"  ✅ {node.name} deleted.")


def handle_existing_vms(
    proxmox: ProxmoxAPI, cluster: ClusterConfig
) -> list[NodeConfig]:
    """Prompt the user when target VMIDs already exist. Returns nodes to create."""
    existing = get_existing_vmids(proxmox, cluster.proxmox.node)
    conflicting = [n for n in cluster.nodes if n.vmid in existing]
    if not conflicting:
        return cluster.nodes

    names = ", ".join(f"{n.name} ({n.vmid})" for n in conflicting)
    print(f"  Existing VMs found: {names}\n")
    print("  [c] Continue  - skip existing VMs and proceed")
    print("  [f] Fresh     - delete all existing VMs and start over")
    print("  [a] Abort")
    print()
    while True:
        choice = input("  Choice [c/f/a]: ").strip().lower()
        if choice in ("c", "f", "a"):
            break
        print("  Please enter c, f, or a.")

    print()
    if choice == "a":
        print("  Aborted.")
        sys.exit(0)

    if choice == "f":
        for node in conflicting:
            try:
                delete_vm(proxmox, node, cluster.proxmox.node)
            except Exception as e:  # noqa: BLE001
                print(f"  ❌ Failed to delete {node.name}: {e}", file=sys.stderr)
                sys.exit(1)
        time.sleep(2)
        return cluster.nodes

    # continue: only create nodes that don't already exist
    existing_ids = {n.vmid for n in conflicting}
    return [n for n in cluster.nodes if n.vmid not in existing_ids]


def start_vm(proxmox: ProxmoxAPI, node: NodeConfig, proxmox_node: str) -> None:
    status = proxmox.nodes(proxmox_node).qemu(node.vmid).status.current.get()  # type: ignore[union-attr]
    if status["status"] == "running":  # type: ignore[index]
        print(f"  ⏭️  {node.name} already running.")
        return
    print(f"  Starting {node.name}...")
    proxmox.nodes(proxmox_node).qemu(node.vmid).status.start.post()
    print(f"  ✅ {node.name} started.")


# --- Talos helpers ---

def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def discover_node_ip(
    proxmox: ProxmoxAPI, node: NodeConfig, proxmox_node: str, timeout: int = 180
) -> str:
    """Poll the QEMU guest agent until the node reports a non-loopback IPv4."""
    print(f"  Discovering current IP for {node.name} via QEMU agent...")
    deadline = time.monotonic() + timeout
    delay = 5.0
    while time.monotonic() < deadline:
        try:
            ifaces = proxmox.nodes(proxmox_node).qemu(node.vmid).agent("network-get-interfaces").get()  # type: ignore[union-attr]
            for iface in (ifaces or {}).get("result", []):
                if iface.get("name") == "lo":
                    continue
                for addr in iface.get("ip-addresses", []):
                    if addr.get("ip-address-type") == "ipv4":
                        ip = addr["ip-address"]
                        print(f"  ✅ {node.name} → {ip}")
                        return ip
        except Exception:  # noqa: BLE001, S110 — QEMU agent can fail transiently during boot
            pass
        time.sleep(delay)
        delay = min(delay * 1.5, 30)
    raise TimeoutError(f"Could not discover IP for {node.name} via QEMU agent within {timeout}s")


def wait_for_maintenance_mode(ip: str, timeout: int = 300) -> None:
    print(f"  Waiting for {ip} to reach Talos maintenance mode...")
    deadline = time.monotonic() + timeout
    delay = 5.0
    while time.monotonic() < deadline:
        result = _run(["talosctl", "version", "--insecure", "--nodes", ip], check=False)
        if result.returncode == 0:
            print(f"  ✅ {ip} is in maintenance mode.")
            return
        time.sleep(delay)
        delay = min(delay * 1.5, 30)
    raise TimeoutError(f"{ip} did not reach maintenance mode within {timeout}s")


def wait_for_reboot(node: NodeConfig, talosconfig: Path, timeout: int = 300) -> None:
    """Wait for the node to come up on its static IP after machine config is applied."""
    print(f"  Waiting for {node.name} to reboot to {node.ip}...")
    deadline = time.monotonic() + timeout
    delay = 10.0
    while time.monotonic() < deadline:
        result = _run(
            ["talosctl", "version", "--nodes", node.ip, "--endpoints", node.ip,
             "--talosconfig", str(talosconfig)],
            check=False,
        )
        if result.returncode == 0:
            print(f"  ✅ {node.name} is up at {node.ip}.")
            return
        time.sleep(delay)
        delay = min(delay * 1.5, 30)
    raise TimeoutError(f"{node.name} did not come up at {node.ip} within {timeout}s")


def _write_node_patch(node: NodeConfig, network: NetworkConfig, talos_config_dir: Path) -> Path:
    patch_dir = talos_config_dir / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_path = patch_dir / f"{node.vmid}.yaml"
    patch = {
        "machine": {
            "network": {
                "interfaces": [
                    {
                        "interface": network.interface,
                        "addresses": [f"{node.ip}/{network.subnet_prefix}"],
                        "routes": [{"network": "0.0.0.0/0", "gateway": network.gateway}],
                        "dhcp": False,
                    }
                ],
                "nameservers": network.nameservers,
            }
        }
    }
    patch_path.write_text(yaml.dump(patch, default_flow_style=False))
    return patch_path


def gen_talos_config(cluster: ClusterConfig) -> None:
    cluster.talos_config_dir.mkdir(parents=True, exist_ok=True)
    print(f"  Generating Talos config for cluster '{cluster.name}'...")
    _run([
        "talosctl", "gen", "config",
        cluster.name,
        f"https://{cluster.endpoint}:6443",
        "--output-dir", str(cluster.talos_config_dir),
        "--talos-version", cluster.talos_version,
        "--force",
    ])
    print("  ✅ Config generated.")
    print("  Writing per-node network patches...")
    for node in cluster.nodes:
        _write_node_patch(node, cluster.network, cluster.talos_config_dir)
    print("  ✅ Patches written.")


def apply_talos_config(
    node: NodeConfig, cluster: ClusterConfig, maintenance_ip: str
) -> None:
    config_file = cluster.talos_config_dir / (
        "controlplane.yaml" if node.role == "controlplane" else "worker.yaml"
    )
    patch_file = cluster.talos_config_dir / "patches" / f"{node.vmid}.yaml"
    print(f"  Applying config to {node.name} (via {maintenance_ip} → static {node.ip})...")
    _run([
        "talosctl", "apply-config",
        "--insecure",
        "--nodes", maintenance_ip,
        "--file", str(config_file),
        "--config-patch", f"@{patch_file}",
    ])
    print(f"  ✅ Config applied to {node.name}.")


def bootstrap_cluster(cluster: ClusterConfig) -> None:
    cp = cluster.control_plane_nodes[0]
    talosconfig = cluster.talos_config_dir / "talosconfig"
    print(f"  Bootstrapping etcd on {cp.name} ({cp.ip})...")
    _run([
        "talosctl", "bootstrap",
        "--nodes", cp.ip,
        "--endpoints", cp.ip,
        "--talosconfig", str(talosconfig),
    ])
    print("  ✅ etcd bootstrapped.")


def wait_for_kubernetes(cluster: ClusterConfig, timeout: int = 600) -> None:
    cp = cluster.control_plane_nodes[0]
    talosconfig = cluster.talos_config_dir / "talosconfig"
    print("  Running cluster health check (may take several minutes)...")
    # talosctl health streams progress to stdout; don't capture so the user sees it
    subprocess.run(
        [
            "talosctl", "health",
            "--nodes", cp.ip,
            "--endpoints", cp.ip,
            "--talosconfig", str(talosconfig),
            "--wait-timeout", f"{timeout}s",
        ],
        check=True,
    )
    print("  ✅ Cluster is healthy.")


def get_kubeconfig(cluster: ClusterConfig) -> None:
    cp = cluster.control_plane_nodes[0]
    talosconfig = cluster.talos_config_dir / "talosconfig"
    print("  Retrieving kubeconfig...")
    _run([
        "talosctl", "kubeconfig",
        "--nodes", cp.ip,
        "--endpoints", cp.ip,
        "--talosconfig", str(talosconfig),
        "--force",
    ])
    print("  ✅ kubeconfig merged into ~/.kube/config.")


# --- MetalLB ---

def install_metallb(cluster: ClusterConfig) -> None:
    mb = cluster.metallb
    url = (f"https://raw.githubusercontent.com/metallb/metallb"
           f"/{mb.version}/config/manifests/metallb-native.yaml")
    print(f"  Applying MetalLB {mb.version}...")
    _run(["kubectl", "apply", "-f", url])
    print("  Waiting for MetalLB pods to be ready...")
    subprocess.run(
        ["kubectl", "wait", "--namespace", "metallb-system",
         "--for=condition=Ready", "pod", "--selector=app=metallb",
         "--timeout=120s"],
        check=True,
    )
    print("  ✅ MetalLB ready.")


def configure_metallb(cluster: ClusterConfig) -> None:
    mb = cluster.metallb
    docs = [
        {
            "apiVersion": "metallb.io/v1beta1",
            "kind": "IPAddressPool",
            "metadata": {"name": mb.pool_name, "namespace": "metallb-system"},
            "spec": {"addresses": [mb.lb_address]},
        },
        {
            "apiVersion": "metallb.io/v1beta1",
            "kind": "L2Advertisement",
            "metadata": {"name": mb.pool_name, "namespace": "metallb-system"},
            "spec": {"ipAddressPools": [mb.pool_name]},
        },
    ]
    combined = yaml.dump_all(docs, default_flow_style=False)
    subprocess.run(["kubectl", "apply", "-f", "-"], input=combined, text=True, check=True)
    print(f"  ✅ IPAddressPool '{mb.pool_name}': {mb.lb_address}")


def verify_metallb(cluster: ClusterConfig) -> None:
    mb = cluster.metallb
    expected_ip = mb.lb_address.split("/")[0]
    test_svc: dict = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": "metallb-lb-verify", "namespace": "default"},
        "spec": {
            "type": "LoadBalancer",
            "ports": [{"protocol": "TCP", "port": 80, "targetPort": 80}],
        },
    }
    subprocess.run(["kubectl", "apply", "-f", "-"],
                   input=yaml.dump(test_svc), text=True, check=True, capture_output=True)
    try:
        print(f"  Waiting for LoadBalancer IP (expected {expected_ip})...")
        deadline = time.monotonic() + 60
        assigned = ""
        while time.monotonic() < deadline:
            result = _run(
                ["kubectl", "get", "svc", "metallb-lb-verify", "-n", "default",
                 "-o", "jsonpath={.status.loadBalancer.ingress[0].ip}"],
                check=False,
            )
            if result.stdout.strip():
                assigned = result.stdout.strip()
                break
            time.sleep(3)
        if assigned == expected_ip:
            print(f"  ✅ MetalLB assigned expected IP: {assigned}")
        elif assigned:
            print(f"  ⚠️  MetalLB assigned {assigned} — expected {expected_ip}")
            print("     Check IPAddressPool range in talos.yaml.")
        else:
            print("  ⚠️  No IP assigned within 60s — MetalLB may still be initialising.")
    finally:
        _run(["kubectl", "delete", "svc", "metallb-lb-verify", "-n", "default"], check=False)


# --- Dry run ---

def dry_run(cluster: ClusterConfig) -> None:
    px = cluster.proxmox
    print(f"=== DRY RUN: {cluster.name} ===\n")
    print(f"Proxmox      : {px.host}  node={px.node}")
    print(f"ISO          : {px.iso_path}")
    print(f"Storage pool : {px.storage_pool}")
    print(f"Network      : bridge={px.bridge}  vlan={px.vlan_tag}  gw={cluster.network.gateway}")
    print(f"Cluster API  : https://{cluster.endpoint}:6443")
    print(f"Talos version: {cluster.talos_version}")
    print(f"Config dir   : {cluster.talos_config_dir}\n")
    print("Nodes:")
    for node in cluster.nodes:
        print(
            f"  vmid={node.vmid}  {node.name:<20} role={node.role:<14} "
            f"ip={node.ip:<16} mem={node.memory}MB  cores={node.cores}  disk={node.disk_gb}GB"
        )
    print("\nPhases:")
    phases = [
        "1. Create VMs in Proxmox",
        "2. Start VMs",
        "3. Discover each node's current IP via QEMU guest agent (DHCP)",
        "4. Wait for Talos maintenance mode on each node",
        "5. Generate Talos cluster config + per-node network patches",
        "6. Apply config (delivers static IP); nodes reboot",
        "7. Wait for each node to come up on its static IP",
        "8. Bootstrap etcd on first control-plane node",
        "9. Wait for cluster health (talosctl health)",
        "10. Retrieve kubeconfig",
        f"11. Install MetalLB {cluster.metallb.version}",
        f"12. Configure IPAddressPool '{cluster.metallb.pool_name}': {cluster.metallb.lb_address}",
        "13. Verify LoadBalancer IP assignment",
    ]
    for phase in phases:
        print(f"  {phase}")


# --- Orchestration ---

def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy Talos Linux cluster on Proxmox")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without executing")
    parser.add_argument("--config", default="talos.yaml", help="Path to talos.yaml")
    parser.add_argument("--metallb-only", action="store_true",
                        help="Skip Talos phases and only install/configure MetalLB on an existing cluster")
    args = parser.parse_args()

    check_talosctl()

    cluster = load_config(args.config)

    if args.dry_run:
        dry_run(cluster)
        return

    if args.metallb_only:
        print(f"=== MetalLB only: {cluster.name} ===")
        print("\n=== Phase 11: Installing MetalLB ===")
        try:
            install_metallb(cluster)
        except subprocess.CalledProcessError as e:
            print(f"  ❌ MetalLB install failed: {e}", file=sys.stderr)
            sys.exit(1)
        print("\n=== Phase 12: Configuring MetalLB IP pool ===")
        try:
            configure_metallb(cluster)
        except subprocess.CalledProcessError as e:
            print(f"  ❌ MetalLB configuration failed: {e}", file=sys.stderr)
            sys.exit(1)
        print("\n=== Phase 13: Verifying MetalLB ===")
        verify_metallb(cluster)
        print(f"\n✅ MetalLB ready. LoadBalancer IP pool: {cluster.metallb.lb_address}")
        return

    proxmox = connect_proxmox(cluster.proxmox)

    print("=== Phase 1: Creating VMs ===")
    nodes_to_create = handle_existing_vms(proxmox, cluster)
    for node in nodes_to_create:
        try:
            create_vm(proxmox, node, cluster.proxmox)
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ Failed to create {node.name}: {e}", file=sys.stderr)
            sys.exit(1)
    time.sleep(2)

    print("\n=== Phase 2: Starting VMs ===")
    for node in cluster.nodes:
        try:
            start_vm(proxmox, node, cluster.proxmox.node)
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ Failed to start {node.name}: {e}", file=sys.stderr)
            sys.exit(1)

    print("\n=== Phase 3: Discovering node IPs ===")
    maintenance_ips: dict[int, str] = {}
    for node in cluster.nodes:
        try:
            maintenance_ips[node.vmid] = discover_node_ip(proxmox, node, cluster.proxmox.node)
        except TimeoutError as e:
            print(f"  ❌ {e}", file=sys.stderr)
            sys.exit(1)

    print("\n=== Phase 4: Waiting for maintenance mode ===")
    for node in cluster.nodes:
        try:
            wait_for_maintenance_mode(maintenance_ips[node.vmid])
        except TimeoutError as e:
            print(f"  ❌ {e}", file=sys.stderr)
            sys.exit(1)

    print("\n=== Phase 5: Generating Talos config ===")
    try:
        gen_talos_config(cluster)
    except subprocess.CalledProcessError as e:
        print(f"  ❌ talosctl gen config failed: {e.stderr}", file=sys.stderr)
        sys.exit(1)

    print("\n=== Phase 6: Applying config to nodes ===")
    for node in cluster.nodes:
        try:
            apply_talos_config(node, cluster, maintenance_ips[node.vmid])
        except subprocess.CalledProcessError as e:
            print(f"  ❌ Failed to apply config to {node.name}: {e.stderr}", file=sys.stderr)
            sys.exit(1)

    print("\n=== Phase 7: Waiting for nodes to reboot to static IPs ===")
    talosconfig = cluster.talos_config_dir / "talosconfig"
    for node in cluster.nodes:
        try:
            wait_for_reboot(node, talosconfig)
        except TimeoutError as e:
            print(f"  ❌ {e}", file=sys.stderr)
            sys.exit(1)

    print("\n=== Phase 8: Bootstrapping cluster ===")
    try:
        bootstrap_cluster(cluster)
    except subprocess.CalledProcessError as e:
        print(f"  ❌ Bootstrap failed: {e.stderr}", file=sys.stderr)
        sys.exit(1)

    print("\n=== Phase 9: Waiting for Kubernetes ===")
    try:
        wait_for_kubernetes(cluster)
    except subprocess.CalledProcessError as e:
        print(f"  ❌ Health check failed: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n=== Phase 10: Retrieving kubeconfig ===")
    try:
        get_kubeconfig(cluster)
    except subprocess.CalledProcessError as e:
        print(f"  ❌ Failed to get kubeconfig: {e.stderr}", file=sys.stderr)
        sys.exit(1)

    print("\n=== Phase 11: Installing MetalLB ===")
    try:
        install_metallb(cluster)
    except subprocess.CalledProcessError as e:
        print(f"  ❌ MetalLB install failed: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n=== Phase 12: Configuring MetalLB IP pool ===")
    try:
        configure_metallb(cluster)
    except subprocess.CalledProcessError as e:
        print(f"  ❌ MetalLB configuration failed: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n=== Phase 13: Verifying MetalLB ===")
    verify_metallb(cluster)

    print(f"\n✅ Cluster '{cluster.name}' deployed successfully.")
    print(f"   LoadBalancer IP pool: {cluster.metallb.lb_address}")


if __name__ == "__main__":
    main()
