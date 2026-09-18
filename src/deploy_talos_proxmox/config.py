from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()


@dataclass
class NodeConfig:
    vmid: int
    name: str
    role: str
    ip: str
    memory: int
    cores: int
    disk_gb: int


@dataclass
class NetworkConfig:
    gateway: str
    interface: str
    nameservers: list[str]
    subnet_prefix: int


@dataclass
class ProxmoxConfig:
    host: str
    node: str
    iso_path: str
    storage_pool: str
    bridge: str
    vlan_tag: int
    user: str
    token_name: str
    token_value: str


@dataclass
class MetalLBConfig:
    lb_address: str
    pool_name: str
    version: str


@dataclass
class ClusterConfig:
    name: str
    endpoint: str
    talos_version: str
    talos_config_dir: Path
    proxmox: ProxmoxConfig
    network: NetworkConfig
    nodes: list[NodeConfig]
    metallb: MetalLBConfig

    @property
    def control_plane_nodes(self) -> list[NodeConfig]:
        return [n for n in self.nodes if n.role == "controlplane"]

    @property
    def worker_nodes(self) -> list[NodeConfig]:
        return [n for n in self.nodes if n.role == "worker"]


def load_config(config_path: str | Path = "talos.yaml") -> ClusterConfig:
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    px = raw["proxmox"]
    proxmox = ProxmoxConfig(
        host=px["host"],
        node=px["node"],
        iso_path=px["iso_path"],
        storage_pool=px["storage_pool"],
        bridge=px["bridge"],
        vlan_tag=px["vlan_tag"],
        user=os.environ["PROXMOX_USER"],
        token_name=os.environ["PROXMOX_TOKEN_NAME"],
        token_value=os.environ["PROXMOX_TOKEN_SECRET"],
    )

    net = raw["network"]
    network = NetworkConfig(
        gateway=net["gateway"],
        interface=net["interface"],
        nameservers=net["nameservers"],
        subnet_prefix=net["subnet_prefix"],
    )

    nodes = [NodeConfig(**n) for n in raw["nodes"]]

    mb = raw.get("metallb", {})
    metallb = MetalLBConfig(
        lb_address=mb.get("lb_address", "10.0.0.1/32"),
        pool_name=mb.get("pool_name", "default"),
        version=mb.get("version", "v0.14.9"),
    )

    c = raw["cluster"]
    return ClusterConfig(
        name=c["name"],
        endpoint=c["endpoint"],
        talos_version=c["talos_version"],
        talos_config_dir=Path(os.getenv("TALOS_CONFIG_DIR", "./config-talos")),
        proxmox=proxmox,
        network=network,
        nodes=nodes,
        metallb=metallb,
    )
