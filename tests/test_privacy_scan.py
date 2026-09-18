"""Tests for the privacy scanner.

This file is excluded from the scan in .privacy-scan.toml, because the
fixtures below are synthetic leaks by design and would otherwise report
themselves.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from privacy_scan import (
    CONFIG_FILE,
    Allowlist,
    build_rules,
    load_allowlist,
    read_worktree,
    scan,
    scan_text,
    tracked_paths,
)

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def allow() -> Allowlist:
    return load_allowlist(CONFIG_FILE)


def rules_for(allow: Allowlist):
    return build_rules(allow)


def matches(text: str, allow: Allowlist) -> set[tuple[str, str]]:
    return {(f.rule, f.match) for f in scan_text("sample", text, rules_for(allow))}


# --- Detection ---------------------------------------------------------------


def test_detects_rfc1918_address(allow: Allowlist) -> None:
    assert ("ipv4", "172.16.5.4") in matches("mgmt is 172.16.5.4", allow)


def test_detects_routable_address(allow: Allowlist) -> None:
    assert ("ipv4", "93.184.216.34") in matches("peer 93.184.216.34", allow)


def test_detects_ipv6_address(allow: Allowlist) -> None:
    assert ("ipv6", "2606:4700:4700::1111") in matches("v6 2606:4700:4700::1111", allow)


def test_detects_mac_address(allow: Allowlist) -> None:
    assert ("mac-address", "aa:bb:cc:dd:ee:ff") in matches("nic aa:bb:cc:dd:ee:ff", allow)


def test_detects_foreign_email(allow: Allowlist) -> None:
    assert ("email", "ops@acme.example") in matches("contact ops@acme.example", allow)


def test_detects_private_key_header(allow: Allowlist) -> None:
    found = matches("-----BEGIN RSA PRIVATE KEY-----", allow)
    assert any(rule == "private-key" for rule, _ in found)


def test_detects_real_talos_node_name(allow: Allowlist) -> None:
    """Talos suffixes node names randomly, so a real one identifies a cluster."""
    assert ("talos-node-name", "talos-w7q-x2k") in matches("node talos-w7q-x2k", allow)


def test_detects_terms_from_the_terms_file(tmp_path: Path) -> None:
    terms = tmp_path / ".privacy-terms"
    terms.write_text("# comment\nContoso\n", encoding="utf-8")
    config = tmp_path / ".privacy-scan.toml"
    config.write_text('[scan]\nterms_file = ".privacy-terms"\n', encoding="utf-8")

    found = matches("deployed at Contoso last week", load_allowlist(config))
    assert ("private-term", "Contoso") in found


# --- Allowlisting ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "endpoint 192.168.0.10",  # example range used across the docs
        "pod cidr 10.244.0.38",  # Flannel, quoted in the source-IP explanation
        "resolver 8.8.8.8",  # public resolver, belongs to no customer
        "doc range 203.0.113.9",  # RFC 5737
        "loopback 127.0.0.1",
        "listen 0.0.0.0",
        "author adearaujo@kentik.com",
        "placeholder your-email@company.com",
        "node talos-abc-xyz",
    ],
)
def test_allowed_values_are_quiet(text: str, allow: Allowlist) -> None:
    assert not matches(text, allow)


@pytest.mark.parametrize(
    "text",
    [
        "chart kagent-1.1.0 and talos v1.13.8",
        "requires uv_build>=0.12.1,<0.13.0",
        "port mapping 9995:9995 at 12:34:56",
    ],
)
def test_version_and_port_strings_are_not_addresses(text: str, allow: Allowlist) -> None:
    assert not matches(text, allow)


def test_pragma_suppresses_a_line(allow: Allowlist) -> None:
    assert not matches("mgmt 172.16.5.4  # privacy-scan: allow", allow)


# --- Repository state --------------------------------------------------------


def test_tracked_files_are_clean(allow: Allowlist) -> None:
    findings = list(scan(tracked_paths(), read_worktree, allow))
    assert not findings, [f"{f.path}:{f.line_no} {f.match}" for f in findings]


def test_private_terms_file_is_gitignored() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".privacy-terms"], cwd=REPO, check=False
    )
    assert result.returncode == 0, ".privacy-terms must never be committable"
