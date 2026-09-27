"""Find the DNS resolvers this computer is set up to use: the "System" entry in the resolver list.

Detection runs when a config is created or reset, and when the user asks for it (``dns-bench config
--detect``, or "Add system resolvers" in Settings). The addresses found are written into the config.
They are not looked up again for every run: a laptop that moves between networks would otherwise mix
two networks' resolvers under one name, and the combined view would average them together.

* macOS: the default resolver in ``scutil --dns``, which is the first block with no ``domain`` line.
  The other blocks only handle one domain each (mDNS's ``local``, VPN split DNS).
* Linux and other POSIX systems: the ``nameserver`` lines in /etc/resolv.conf. When those are only a
  loopback stub (systemd-resolved's 127.0.0.53), the servers the stub forwards to are read from
  /run/systemd/resolve/resolv.conf instead.

Loopback addresses are always left out. A stub forwards to the real resolvers, so timing it would
measure a cache on this computer, not the network's resolvers.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

RESOLV_CONF = Path("/etc/resolv.conf")
SYSTEMD_RESOLV_CONF = Path("/run/systemd/resolve/resolv.conf")
SCUTIL_TIMEOUT_S = 5.0

_SCUTIL_NAMESERVER_RE = re.compile(r"^\s*nameserver\[\d+\]\s*:\s*(\S+)\s*$")
_SCUTIL_IFINDEX_RE = re.compile(r"^\s*if_index\s*:\s*\d+\s*\((\S+)\)\s*$")
_SCUTIL_DOMAIN_RE = re.compile(r"^\s*domain\s*:")


@dataclass
class Detected:
    """What detection found. ``servers`` is empty when nothing usable was found."""

    servers: list[str]  # usable addresses, in the system's order, without duplicates
    source: str | None = None  # where they came from, e.g. "scutil --dns" or "/etc/resolv.conf"
    skipped: list[str] = field(default_factory=list)  # found but left out (loopback stubs, junk)

    def describe_empty(self) -> str:
        """Why ``servers`` is empty, worded for the user."""
        if self.skipped:
            listed = ", ".join(self.skipped)
            return f"{self.source or 'the system'} lists only {listed}, which can't be benchmarked"
        if self.source:
            return f"{self.source} lists no DNS servers"
        return "this system's DNS settings could not be read"


def parse_scutil(text: str) -> list[str]:
    """Nameservers of the default resolver in ``scutil --dns`` output: the first block, in the first
    section, that has no ``domain`` line and lists at least one server.

    Only that one block counts. Another domain-less block (a VPN's, say) belongs to another network,
    and one System entry must never mix two networks' resolvers. The "(for scoped queries)" section
    repeats servers per interface, so it is skipped. A link-local IPv6 server without a zone gets its
    block's interface, so it can be reached.
    """
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("DNS configuration") and blocks:
            break  # the scoped section
        if line.startswith("resolver #"):
            blocks.append([])
        elif blocks:
            blocks[-1].append(line)
    for block in blocks:
        if any(_SCUTIL_DOMAIN_RE.match(line) for line in block):
            continue
        interface = next((m.group(1) for line in block if (m := _SCUTIL_IFINDEX_RE.match(line))), None)
        servers = []
        for line in block:
            m = _SCUTIL_NAMESERVER_RE.match(line)
            if not m:
                continue
            server = m.group(1)
            if interface and "%" not in server and _is_link_local_v6(server):
                server = f"{server}%{interface}"
            servers.append(server)
        if servers:
            return servers
    return []


def parse_resolv_conf(text: str) -> list[str]:
    """The ``nameserver`` addresses in a resolv.conf, in order."""
    servers = []
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split(";", 1)[0].split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            servers.append(fields[1])
    return servers


def _is_link_local_v6(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.version == 6 and ip.is_link_local


def _usable(found: list[str]) -> tuple[list[str], list[str]]:
    """Split addresses into (usable, skipped). Usable ones are canonical and unique."""
    usable: list[str] = []
    skipped: list[str] = []
    for value in found:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            skipped.append(value)
            continue
        text = str(ip)
        if ip.is_loopback or (ip.version == 6 and ip.is_link_local and ip.scope_id is None):
            skipped.append(text)  # a local stub, or an address that can't be reached without a zone
        elif text not in usable:
            usable.append(text)
    return usable, list(dict.fromkeys(skipped))


def _run_scutil() -> str | None:
    try:
        done = subprocess.run(
            ["scutil", "--dns"], capture_output=True, text=True, timeout=SCUTIL_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError):  # not installed, not runnable, or hung
        return None
    return done.stdout if done.returncode == 0 else None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def detect(
    platform: str | None = None,
    run_scutil: Callable[[], str | None] = _run_scutil,
    read: Callable[[Path], str | None] = _read,
) -> Detected:
    """The resolvers this computer uses. Never raises: an unreadable setup gives no servers."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        text = run_scutil()
        if text is not None:
            servers, skipped = _usable(parse_scutil(text))
            if servers:
                return Detected(servers, "scutil --dns", skipped)
        # scutil failed or found nothing usable: macOS keeps /etc/resolv.conf up to date too
    text = read(RESOLV_CONF)
    if text is None:
        return Detected([])
    servers, skipped = _usable(parse_resolv_conf(text))
    if not servers and skipped:
        upstream = read(SYSTEMD_RESOLV_CONF)
        if upstream is not None:
            up_servers, up_skipped = _usable(parse_resolv_conf(upstream))
            if up_servers:
                return Detected(up_servers, str(SYSTEMD_RESOLV_CONF), skipped + up_skipped)
    return Detected(servers, str(RESOLV_CONF), skipped)
