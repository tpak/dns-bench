"""A tiny pure-Python UDP DNS client, just enough to time one query.

No dependency on ``dig``. One fresh UDP socket per attempt (random source
port), random 16-bit query ID, and only replies that match the ID, have QR=1
and come from the server we asked are accepted.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
import time
from dataclasses import asdict, dataclass

QTYPES = {"A": 1, "AAAA": 28}
QCLASS_IN = 1
# fmt: off
RCODES = {
    0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP",
    5: "REFUSED", 6: "YXDOMAIN", 7: "YXRRSET", 8: "NXRRSET", 9: "NOTAUTH", 10: "NOTZONE",
}
# fmt: on
OK_RCODES = ("NOERROR", "NXDOMAIN")
DNS_PORT = 53
_HEADER = struct.Struct("!HHHHHH")


@dataclass
class QueryResult:
    # fmt: off
    status: str                 # "ok" | "timeout" | "error"
    ms: float | None = None     # round trip of the answering attempt
    rcode: str | None = None    # "NOERROR", "NXDOMAIN", "SERVFAIL", ...
    answers: int = 0            # ANCOUNT
    error: str | None = None
    attempts: int = 0
    truncated: bool = False     # TC bit set (latency still valid)
    # fmt: on

    def to_dict(self) -> dict:
        return asdict(self)


def new_query_id() -> int:
    """Random 16-bit query ID from the OS CSPRNG (same source as secrets.randbits;
    avoids importing hashlib, which logs errors on some broken Python builds)."""
    return int.from_bytes(os.urandom(2), "big")


def _encode_name(domain: str) -> bytes:
    name = domain.strip().rstrip(".")
    if not name:
        raise ValueError("empty domain name")
    try:
        raw = name.encode("ascii")
    except UnicodeEncodeError:
        raw = name.encode("idna")
    out = bytearray()
    for label in raw.split(b"."):
        if not 1 <= len(label) <= 63:
            raise ValueError(f"invalid label length in {domain!r}")
        out.append(len(label))
        out += label
    out.append(0)
    if len(out) > 255:
        raise ValueError(f"domain name too long: {domain!r}")
    return bytes(out)


def build_query(qid: int, domain: str, qtype="A") -> bytes:
    """Build a standard recursive query: header (RD=1, QDCOUNT=1) + question."""
    if isinstance(qtype, str):
        try:
            qtype = QTYPES[qtype.upper()]
        except KeyError:
            raise ValueError(f"unsupported record type {qtype!r}") from None
    if not 0 <= qid <= 0xFFFF:
        raise ValueError("query id must fit in 16 bits")
    flags = 0x0100  # QR=0 opcode=QUERY RD=1
    header = _HEADER.pack(qid, flags, 1, 0, 0, 0)
    return header + _encode_name(domain) + struct.pack("!HH", qtype, QCLASS_IN)


def parse_response(data: bytes) -> dict:
    """Parse the 12-byte DNS header. Raises ValueError if too short."""
    if len(data) < 12:
        raise ValueError("DNS message shorter than 12-byte header")
    qid, flags, qd, an, ns, ar = _HEADER.unpack_from(data, 0)
    rcode = flags & 0x000F
    return {
        "id": qid,
        "qr": bool(flags & 0x8000),
        "opcode": (flags >> 11) & 0x0F,
        "aa": bool(flags & 0x0400),
        "tc": bool(flags & 0x0200),
        "rd": bool(flags & 0x0100),
        "ra": bool(flags & 0x0080),
        "rcode": rcode,
        "rcode_name": RCODES.get(rcode, f"RCODE{rcode}"),
        "qdcount": qd,
        "ancount": an,
        "nscount": ns,
        "arcount": ar,
    }


def _strip_scope(host: str) -> str:
    return host.split("%", 1)[0]


def addr_matches(src_host: str, server_ip) -> bool:
    """True if a recvfrom() source address is the server we queried."""
    try:
        ip = ipaddress.ip_address(_strip_scope(src_host))
    except ValueError:
        return False
    target = server_ip if not isinstance(server_ip, str) else ipaddress.ip_address(_strip_scope(server_ip))
    if ip == target:
        return True
    # IPv4-mapped IPv6 (::ffff:a.b.c.d) vs plain IPv4
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped is not None and mapped == target


def _attempt(sockaddr, family, target_ip, port, packet, qid, timeout_s) -> QueryResult:
    try:
        sock = socket.socket(family, socket.SOCK_DGRAM)
    except OSError as exc:
        return QueryResult("error", error=f"socket: {exc}")
    with sock:
        try:
            start = time.perf_counter()
            sock.sendto(packet, sockaddr)
        except OSError as exc:
            return QueryResult("error", error=f"send: {exc}")
        deadline = start + timeout_s
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return QueryResult("timeout", error="timeout")
            sock.settimeout(remaining)
            try:
                data, src = sock.recvfrom(4096)
            except socket.timeout:
                return QueryResult("timeout", error="timeout")
            except OSError as exc:  # e.g. ICMP port unreachable
                return QueryResult("error", error=f"recv: {exc}")
            now = time.perf_counter()
            if src[1] != port or not addr_matches(src[0], target_ip):
                continue  # not from our server: ignore
            try:
                hdr = parse_response(data)
            except ValueError:
                continue
            if hdr["id"] != qid or not hdr["qr"]:
                continue  # stray / spoofed / stale packet
            ms = (now - start) * 1000.0
            rcode = hdr["rcode_name"]
            status = "ok" if rcode in OK_RCODES else "error"
            return QueryResult(
                status=status,
                ms=ms,
                rcode=rcode,
                answers=hdr["ancount"],
                error=None if status == "ok" else rcode,
                truncated=hdr["tc"],
            )


def query(
    server: str,
    domain: str,
    record_type: str = "A",
    timeout_s: float = 1.0,
    tries: int = 1,
    port: int = DNS_PORT,
) -> QueryResult:
    """Send one DNS query (retrying only after a timeout) and time the reply."""
    try:
        target_ip = ipaddress.ip_address(_strip_scope(str(server).strip("[]")))
    except ValueError:
        return QueryResult("error", error=f"invalid server address {server!r}")
    family = socket.AF_INET6 if target_ip.version == 6 else socket.AF_INET
    try:
        infos = socket.getaddrinfo(
            str(server).strip("[]"), port, family, socket.SOCK_DGRAM, 0, socket.AI_NUMERICHOST
        )
        sockaddr = infos[0][4]
    except (OSError, IndexError) as exc:
        return QueryResult("error", error=f"address: {exc}")
    tries = max(1, int(tries))
    result = QueryResult("error", error="no attempt made")
    for attempt in range(1, tries + 1):
        qid = new_query_id()
        try:
            packet = build_query(qid, domain, record_type)
        except ValueError as exc:
            return QueryResult("error", error=str(exc), attempts=attempt - 1)
        result = _attempt(sockaddr, family, target_ip, port, packet, qid, float(timeout_s))
        result.attempts = attempt
        if result.status != "timeout":
            break
    return result
