"""Scan repository content for private information before it reaches a commit.

This is the identity layer of a two-tool split. `gitleaks` already matches
credential *formats* (API keys, tokens, private keys) and is maintained
upstream, so reimplementing it here would be worse than using it. What it
does not catch is data that is private because of what it *refers to*: a
customer's name, a real management IP, a MAC address, a node hostname. That
is what this module finds.

Both run from the pre-commit hook. See the `privacy` and `install-hooks`
targets in the Makefile.

Every rule is checked against an allowlist in `.privacy-scan.toml`, because
this repository publishes example addresses and placeholder emails on
purpose. Terms that are themselves private, such as customer names, live in
an untracked `.privacy-terms` file instead.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO / ".privacy-scan.toml"

# A line carrying this marker is skipped, for the rare documented exception.
PRAGMA = "privacy-scan: allow"

# Binary and lockfile content produces only false positives.
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".lock"}


@dataclass(frozen=True)
class Finding:
    path: str
    line_no: int
    rule: str
    match: str
    reason: str


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    # Returns why the match is a leak, or None when it is allowed.
    check: Callable[[str], str | None]


@dataclass
class Allowlist:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(default_factory=list)
    email_domains: set[str] = field(default_factory=set)
    literals: set[str] = field(default_factory=set)
    exclude_paths: set[str] = field(default_factory=set)
    terms: list[str] = field(default_factory=list)

    def allows_address(self, text: str) -> bool:
        if text in self.literals:
            return True
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            return False
        return any(
            address.version == net.version and address in net for net in self.networks
        )


def load_allowlist(config_file: Path = CONFIG_FILE) -> Allowlist:
    if not config_file.exists():
        return Allowlist()
    with config_file.open("rb") as handle:
        raw = tomllib.load(handle)

    allow = raw.get("allow", {})
    scan = raw.get("scan", {})
    lst = Allowlist(
        networks=[ipaddress.ip_network(c, strict=False) for c in allow.get("networks", [])],
        email_domains={d.lower() for d in allow.get("email_domains", [])},
        literals=set(allow.get("literals", [])),
        exclude_paths=set(scan.get("exclude_paths", [])),
    )

    # Private terms are private themselves, so they are never committed.
    terms_file = config_file.parent / scan.get("terms_file", ".privacy-terms")
    if terms_file.exists():
        lst.terms = [
            line.strip()
            for line in terms_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    return lst


# --- Rules -------------------------------------------------------------------

IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?![\w:])")
MAC = re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}(?![\w:-])")
EMAIL = re.compile(r"[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}")
PEM_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")
JWT = re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")
# Talos generates node names with two random three-character groups.
TALOS_NODE = re.compile(r"\btalos-[a-z0-9]{3}-[a-z0-9]{3}\b")

DOC_NETWORKS = [
    ipaddress.ip_network(n)
    for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
]


def _is_uninteresting(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Addresses that identify nothing: loopback, unspecified, docs, multicast."""
    return (
        address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
        or any(address.version == n.version and address in n for n in DOC_NETWORKS)
    )


def build_rules(allow: Allowlist) -> list[Rule]:
    def check_ip(text: str) -> str | None:
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            return None  # A version string or similar, not an address.
        if _is_uninteresting(address) or allow.allows_address(text):
            return None
        if address.is_private or address.is_link_local:
            return "private address, RFC1918 or equivalent, identifies internal topology"
        return "routable address, may identify a real host"

    def check_mac(text: str) -> str | None:
        if text in allow.literals:
            return None
        return "MAC address, identifies a physical interface"

    def check_email(text: str) -> str | None:
        if text in allow.literals:
            return None
        domain = text.rsplit("@", 1)[-1].lower()
        if domain in allow.email_domains:
            return None
        return "email address"

    def check_literal_rule(reason: str) -> Callable[[str], str | None]:
        return lambda text: None if text in allow.literals else reason

    rules = [
        Rule("ipv4", IPV4, check_ip),
        Rule("ipv6", IPV6, check_ip),
        Rule("mac-address", MAC, check_mac),
        Rule("email", EMAIL, check_email),
        Rule("private-key", PEM_KEY, check_literal_rule("private key material")),
        Rule("jwt", JWT, check_literal_rule("JSON web token")),
        Rule(
            "talos-node-name",
            TALOS_NODE,
            check_literal_rule("real Talos node name, randomly suffixed per cluster"),
        ),
    ]
    if allow.terms:
        pattern = re.compile(
            r"|".join(rf"\b{re.escape(t)}\b" for t in allow.terms), re.IGNORECASE
        )
        rules.append(
            Rule("private-term", pattern, lambda _: "term listed in .privacy-terms")
        )
    return rules


# --- Scanning ----------------------------------------------------------------


def scan_text(path: str, text: str, rules: Iterable[Rule]) -> list[Finding]:
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if PRAGMA in line:
            continue
        for rule in rules:
            for match in rule.pattern.finditer(line):
                value = match.group(0)
                reason = rule.check(value)
                if reason is not None:
                    findings.append(Finding(path, line_no, rule.name, value, reason))
    return findings


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout


def staged_paths() -> list[str]:
    out = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [p for p in out.split("\0") if p]


def tracked_paths() -> list[str]:
    return [p for p in _git("ls-files", "-z").split("\0") if p]


def read_staged(path: str) -> str | None:
    """Read the staged blob, which is what a commit would record."""
    result = subprocess.run(
        ["git", "show", f":{path}"], cwd=REPO, capture_output=True, check=False
    )
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_worktree(path: str) -> str | None:
    try:
        return (REPO / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def scan(
    paths: Iterable[str],
    reader: Callable[[str], str | None],
    allow: Allowlist,
) -> Iterator[Finding]:
    rules = build_rules(allow)
    for path in paths:
        if path in allow.exclude_paths or Path(path).suffix.lower() in SKIP_SUFFIXES:
            continue
        text = reader(path)
        if text is None:
            continue
        yield from scan_text(path, text, rules)


def report(findings: list[Finding]) -> None:
    for finding in findings:
        print(f"{finding.path}:{finding.line_no}: [{finding.rule}] {finding.match}")
        print(f"    {finding.reason}")
    print(f"\n{len(findings)} potential leak(s) found.")
    print(
        "Fix the content, or if it is genuinely safe, allow it in "
        f".privacy-scan.toml or add '# {PRAGMA}' to the line."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--all",
        action="store_true",
        help="scan every tracked file instead of only the staged changes",
    )
    args = parser.parse_args(argv)

    allow = load_allowlist()
    if args.all:
        findings = list(scan(tracked_paths(), read_worktree, allow))
    else:
        findings = list(scan(staged_paths(), read_staged, allow))

    if findings:
        report(findings)
        return 1
    print("  privacy scan OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
