"""Configuration: defaults, normalisation, validation, atomic load/save.

The config file is plain JSON (see README.md for the schema). The web UI
overwrites it on save, so everything that reaches disk goes through
``normalize_config`` + ``validate_config`` first.

Loading never writes. A missing file loads as the defaults plus this computer's own resolvers
(``initial_config``); the file itself is created by an explicit step: saving, resetting, or
``ensure_config`` when a run or the web UI starts. So the "System" entry is detected once and then
stays fixed (see sysdns.py for why).
"""

from __future__ import annotations

import contextlib
import copy
import ipaddress
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import sysdns

# Popular sites across several countries, in a fixed order (README "History" says where they came from).
DEFAULT_DOMAINS = [
    "google.com",
    "bbc.co.uk",
    "reddit.com",
    "amazon.com",
    "github.com",
    "wikipedia.org",
    "apple.com",
    "microsoft.com",
    "netflix.com",
    "spotify.com",
    "twitter.com",
    "linkedin.com",
    "instagram.com",
    "nytimes.com",
    "cnn.com",
    "yahoo.com",
    "ebay.com",
    "paypal.com",
    "stackoverflow.com",
    "dropbox.com",
    "salesforce.com",
    "adobe.com",
    "oracle.com",
    "ibm.com",
    "intel.com",
    "samsung.com",
    "sony.com",
    "nike.com",
    "airbnb.com",
    "uber.com",
    "slack.com",
    "zoom.us",
    "shopify.com",
    "wordpress.com",
    "wikimedia.org",
    "mozilla.org",
    "cloudflare.com",
    "digitalocean.com",
    "heroku.com",
    "atlassian.com",
    "trello.com",
    "notion.so",
    "figma.com",
    "canva.com",
    "twitch.tv",
    "pinterest.com",
    "tumblr.com",
    "quora.com",
    "medium.com",
    "etsy.com",
    "commbank.com.au",
    "anz.com.au",
    "westpac.com.au",
    "nab.com.au",
    "telstra.com.au",
    "optus.com.au",
    "woolworths.com.au",
    "coles.com.au",
    "news.com.au",
    "realestate.com.au",
]

# Public resolvers that anyone can reach. A new config also gets a "System" entry with this computer's
# own resolvers (initial_config), which is usually the ISP's or the router's.
DEFAULT_RESOLVERS = [
    {"name": "OpenDNS", "servers": ["208.67.222.222", "208.67.220.220"], "enabled": True},
    {"name": "Cloudflare", "servers": ["1.1.1.1", "1.0.0.1"], "enabled": True},
    {"name": "Google", "servers": ["8.8.8.8", "8.8.4.4"], "enabled": True},
    {"name": "Quad9", "servers": ["9.9.9.9", "149.112.112.112"], "enabled": False},
]
SYSTEM_NAME = "System"

DEFAULT_SETTINGS = {
    "per_server_interval_ms": 250,
    "timeout_ms": 1000,
    "tries": 1,
    "rounds": 1,
    "slow_threshold_ms": 200,
    "record_type": "A",
    "shuffle": True,
}

DEFAULT_CONFIG = {
    "resolvers": DEFAULT_RESOLVERS,
    "domains": DEFAULT_DOMAINS,
    "settings": DEFAULT_SETTINGS,
}

# Integer settings and their inclusive bounds. The 50 ms interval floor is a
# hard safety limit: never more than 20 queries/s to any single server.
SETTING_BOUNDS = {
    "per_server_interval_ms": (50, 5000),
    "timeout_ms": (200, 10000),
    "tries": (1, 3),
    "rounds": (1, 10),
    "slow_threshold_ms": (1, 10000),
}
RECORD_TYPES = ("A", "AAAA")
MIN_INTERVAL_MS = SETTING_BOUNDS["per_server_interval_ms"][0]
SETTING_UNITS = {"per_server_interval_ms": "ms", "timeout_ms": "ms", "slow_threshold_ms": "ms"}

MAX_DOMAINS = 500
MAX_RESOLVERS = 20
MAX_SERVERS_PER_RESOLVER = 4
MAX_NAME_LEN = 40
MAX_HOSTNAME_LEN = 253  # a DNS name in text form, without the trailing dot (RFC 1035)
# Servers x domains x rounds in one run (the defaults send a few hundred). It bounds a run's duration,
# its file size (a few hundred bytes per query) and what the web UI has to draw.
MAX_QUERIES_PER_RUN = 50_000

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SPLIT_RE = re.compile(r"[\s,;]+")
# ASCII digits only (``str.isdigit`` also accepts e.g. "²", which int() rejects);
# 9 digits is far beyond every bound and far below int()'s 4300-digit limit.
_INT_RE = re.compile(r"-?[0-9]{1,9}")
# IPv6 zone IDs are only accepted on link-local addresses, as an interface name.
_ZONE_RE = re.compile(r"[A-Za-z0-9._-]{1,15}")


@dataclass(frozen=True)
class ValidationError:
    """One problem with a config. ``str()`` is the sentence the CLI prints.

    ``path`` says where, in the normalised config: ``resolvers[0].servers[1]``, ``domains[3]``,
    ``settings.rounds``, a whole section (``resolvers``, ``domains``, ``settings``), or ``""`` for the
    config (or its file) as a whole. List indices count from 0. ``code`` names the kind of problem
    (one of ``ERROR_CODES``), so a program can react without parsing the message. Messages never
    contain the config file's path: the CLI adds it, and the web API doesn't need it.
    """

    path: str
    code: str
    message: str

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


ERROR_CODES = {
    "type": "a value of the wrong JSON type",
    "required": "something that must be there is missing or empty",
    "too_many": "a list over its limit",
    "too_long": "a name over its length limit",
    "invalid": "a value that can't be used, such as an IP address or hostname that doesn't parse",
    "unusable_address": "an IP address that can't be a DNS resolver (multicast, broadcast, ...)",
    "duplicate": "a resolver name or server address used twice",
    "none_enabled": "no resolver is enabled",
    "out_of_range": "a number outside its bounds",
    "invalid_choice": "a value that isn't one of the allowed choices",
    "too_many_queries": "a run over the queries-per-run limit",
    "more_errors": "more problems of the kind just listed, not listed one by one",
    "unreadable": "the config file can't be read",
    "invalid_json": "the config file isn't valid JSON",
    "write_failed": "the config file can't be written",
}


class ConfigError(Exception):
    """Raised for unreadable or invalid configuration.

    ``errors`` is a list of ValidationError. ``file`` is the config file they are about, when there is
    one; ``messages`` are the errors as the CLI prints them, prefixed with it.
    """

    def __init__(self, errors: str | ValidationError | list, file: str | os.PathLike[str] | None = None):
        items = [errors] if isinstance(errors, (str, ValidationError)) else list(errors)
        self.errors: list[ValidationError] = [
            e if isinstance(e, ValidationError) else ValidationError("", "invalid", str(e)) for e in items
        ]
        self.file = Path(file) if file is not None else None
        super().__init__("; ".join(self.messages) if self.errors else "Invalid config")

    @property
    def messages(self) -> list[str]:
        prefix = f"{self.file}: " if self.file is not None else ""
        return [prefix + e.message for e in self.errors]


class ConfigWriteError(ConfigError):
    """The config could not be written (permissions, full disk, ...).

    A subclass so the CLI reports it like any other config problem, while the
    HTTP API can tell a disk failure (500) apart from invalid input (400).
    """


# Resolvers the Settings page offers to add with one click (the defaults are already in the list).
PRESETS = [
    {"name": "Quad9", "servers": ["9.9.9.9", "149.112.112.112"]},
    {"name": "AdGuard", "servers": ["94.140.14.14", "94.140.15.15"]},
    {"name": "Control D", "servers": ["76.76.2.0", "76.76.10.0"]},
    {"name": "CleanBrowsing", "servers": ["185.228.168.9", "185.228.169.9"]},
]


def default_config() -> dict:
    """Return a fresh deep copy of the default configuration."""
    return copy.deepcopy(DEFAULT_CONFIG)


def setting_schema() -> list[dict]:
    """Each setting's type, default and allowed values, in DEFAULT_SETTINGS order (for GET /api/schema)."""
    out: list[dict] = []
    for key, default in DEFAULT_SETTINGS.items():
        if key in SETTING_BOUNDS:
            lo, hi = SETTING_BOUNDS[key]
            out.append(
                {
                    "key": key,
                    "type": "int",
                    "min": lo,
                    "max": hi,
                    "default": default,
                    "unit": SETTING_UNITS.get(key, ""),
                }
            )
        elif key == "record_type":
            out.append({"key": key, "type": "choice", "choices": list(RECORD_TYPES), "default": default})
        else:
            out.append({"key": key, "type": "bool", "default": default})
    return out


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def normalize_domain(value) -> str:
    """strip, lowercase, drop trailing dot, IDNA-encode unicode names."""
    d = str(value).strip().lower().rstrip(".")
    # Encoding takes time in proportion to the input, which can be a whole request body. A name longer
    # than MAX_HOSTNAME_LEN characters stays too long once encoded, so it is left for validation to reject.
    if d and not d.isascii() and len(d) <= MAX_HOSTNAME_LEN:
        with contextlib.suppress(UnicodeError):  # left as-is; validation reports it
            d = d.encode("idna").decode("ascii")
    return d


def normalize_server(value) -> str:
    """Canonicalise an IP literal (e.g. compress IPv6); leave junk untouched.

    IPv4-mapped IPv6 (``::ffff:1.2.3.4``) becomes plain IPv4: it reaches the
    very same host, so it must never count as a separate server. A zone ID
    (``%...``) is kept so validation can judge it.
    """
    s = str(value).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return s
    if ip.version == 6 and ip.ipv4_mapped is not None and ip.scope_id is None:
        return str(ip.ipv4_mapped)
    return str(ip)


def server_key(value) -> str | None:
    """Identity of the host behind a server literal, for de-duplication.

    Different spellings of one address map to the same key: IPv6 compression,
    IPv4-mapped IPv6 and IPv6 zone IDs (``::1`` == ``::1%1``). None if the
    value is not an IP literal.
    """
    s = str(value).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return None
    return _host_key(ip)


def _host_key(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """``server_key`` for an already-parsed address."""
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        if ip.scope_id is not None:
            return str(ipaddress.ip_address(str(ip).split("%", 1)[0]))
    return str(ip)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [p for p in _SPLIT_RE.split(value) if p]
    if isinstance(value, (list, tuple)):
        return list(value)
    return value  # wrong type: leave for validation to report


def _coerce_int(value):
    """Turn integral floats / numeric strings into ints; leave anything else.

    Never raises: strings int() would reject ("--5", "²", 5000 digits) are
    passed through unchanged for validation to report.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        try:
            return int(value.strip())
        except ValueError:  # pragma: no cover - the regex already guarantees this
            return value
    return value


def _short_repr(value, limit: int = 40) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _server_problem(ip) -> str | None:
    """Why a syntactically valid IP can't be a DNS resolver (None if it can)."""
    if (
        ip.version == 6
        and ip.scope_id is not None
        and not (ip.is_link_local and _ZONE_RE.fullmatch(ip.scope_id))
    ):
        return (
            "zone IDs (%...) are only allowed on link-local fe80::/10 addresses "
            "and must be an interface name like en0"
        )
    chk = ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped is not None else ip
    if chk.is_multicast:
        return "is a multicast address, not a DNS resolver"
    if chk.is_unspecified:
        return "is the unspecified address (0.0.0.0 / ::), not a DNS resolver"
    # IPv4 only: Python marks all of IPv6 ::/8 reserved, which includes ::1 and NAT64.
    if chk.version == 4 and chk.is_reserved:
        return "is a reserved/broadcast address, not a DNS resolver"
    return None


def normalize_config(cfg) -> dict:
    """Return a normalised copy of ``cfg`` (best effort, never raises).

    * resolvers: names stripped, servers split/stripped/canonicalised,
      ``enabled`` defaults to true.
    * domains: strip, lowercase, trailing dot removed, blanks dropped,
      de-duplicated preserving order, unicode IDNA-encoded.
    * settings: missing keys filled from defaults, unknown keys dropped (so a
      setting that no longer exists, like max_parallel_servers, just goes away).
    Values of the wrong type are passed through so validation can report them.
    """
    if not isinstance(cfg, dict):
        return cfg
    out: dict[str, Any] = {}

    resolvers = cfg.get("resolvers")
    if isinstance(resolvers, list):
        norm_resolvers = []
        for r in resolvers:
            if not isinstance(r, dict):
                norm_resolvers.append(r)
                continue
            name = r.get("name", "")
            name = name.strip() if isinstance(name, str) else name
            servers = _as_list(r.get("servers"))
            if isinstance(servers, list):
                servers = [normalize_server(s) for s in servers if not (isinstance(s, str) and not s.strip())]
            enabled = r.get("enabled", True)
            norm_resolvers.append({"name": name, "servers": servers, "enabled": enabled})
        out["resolvers"] = norm_resolvers
    else:
        out["resolvers"] = resolvers

    domains = _as_list(cfg.get("domains"))
    if isinstance(domains, list):
        seen = set()
        norm_domains = []
        for d in domains:
            if not isinstance(d, str):
                norm_domains.append(d)
                continue
            nd = normalize_domain(d)
            if nd and nd not in seen:
                seen.add(nd)
                norm_domains.append(nd)
        out["domains"] = norm_domains
    else:
        out["domains"] = domains

    settings = cfg.get("settings")
    if settings is None:
        settings = {}
    if isinstance(settings, dict):
        norm_settings = {}
        for key, default in DEFAULT_SETTINGS.items():
            value = settings.get(key, default)
            if key in SETTING_BOUNDS:
                value = _coerce_int(value)
            elif key == "record_type" and isinstance(value, str):
                value = value.strip().upper()
            norm_settings[key] = value
        out["settings"] = norm_settings
    else:
        out["settings"] = settings
    return out


def duplicate_domains(cfg: object) -> int:
    """How many entries of ``cfg``'s domain list normalising drops as repeats (blank ones don't count)."""
    domains = _as_list(cfg.get("domains")) if isinstance(cfg, dict) else None
    if not isinstance(domains, list):
        return 0
    names = [normalize_domain(d) for d in domains if isinstance(d, str)]
    names = [d for d in names if d]
    return len(names) - len(set(names))


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _hostname_error(domain) -> str | None:
    if not isinstance(domain, str):
        return "must be a string"
    if not domain:
        return "is empty"
    if len(domain) > MAX_HOSTNAME_LEN:
        return f"is longer than {MAX_HOSTNAME_LEN} characters"
    for label in domain.split("."):
        if not label:
            return "has an empty label (two dots in a row?)"
        if len(label) > 63:
            return f"label '{label[:20]}...' is longer than 63 characters"
        if not _LABEL_RE.fullmatch(label):
            return f"label '{label}' may only contain a-z, 0-9 and '-' and must not start or end with '-'"
    return None


def _label(i: int, name: object) -> str:
    """How messages name resolver ``i`` (0-based): "Resolver #1 (Cloudflare)"."""
    if not isinstance(name, str) or not name:
        return f"Resolver #{i + 1}"
    # never echo control characters (terminal escapes) back in messages
    return f"Resolver #{i + 1} ({name if name.isprintable() else _short_repr(name)})"


def _validate_resolvers(resolvers: object) -> list[ValidationError]:
    if not isinstance(resolvers, list) or not resolvers:
        return [ValidationError("resolvers", "required", "resolvers: at least one resolver is required")]
    errors: list[ValidationError] = []
    if len(resolvers) > MAX_RESOLVERS:
        errors.append(
            ValidationError(
                "resolvers",
                "too_many",
                f"resolvers: at most {MAX_RESOLVERS} resolvers allowed (got {len(resolvers)})",
            )
        )
    names_seen: dict[str, int] = {}
    servers_seen: dict[str, str] = {}
    any_enabled = False
    for i, r in enumerate(resolvers):
        at = f"resolvers[{i}]"
        if not isinstance(r, dict):
            errors.append(
                ValidationError(
                    at, "type", f"Resolver #{i + 1}: must be an object with name, servers, enabled"
                )
            )
            continue
        name = r.get("name")
        label = _label(i, name)
        if not isinstance(name, str) or not name:
            errors.append(ValidationError(f"{at}.name", "required", f"{label}: name is required"))
        else:
            if len(name) > MAX_NAME_LEN:
                errors.append(
                    ValidationError(
                        f"{at}.name", "too_long", f"{label}: name must be at most {MAX_NAME_LEN} characters"
                    )
                )
            # isprintable() is False for C0/DEL/C1 controls and bidi overrides
            if "," in name or not name.isprintable():
                errors.append(
                    ValidationError(
                        f"{at}.name",
                        "invalid",
                        f"{label}: name must not contain commas or control characters",
                    )
                )
            key = name.casefold()
            if key in names_seen:
                errors.append(
                    ValidationError(
                        f"{at}.name",
                        "duplicate",
                        f"{label}: duplicate name (same as resolver #{names_seen[key] + 1})",
                    )
                )
            else:
                names_seen[key] = i
        servers = r.get("servers")
        if not isinstance(servers, list) or not servers:
            errors.append(
                ValidationError(f"{at}.servers", "required", f"{label}: at least one server IP is required")
            )
        else:
            if len(servers) > MAX_SERVERS_PER_RESOLVER:
                errors.append(
                    ValidationError(
                        f"{at}.servers",
                        "too_many",
                        f"{label}: at most {MAX_SERVERS_PER_RESOLVER} servers allowed (got {len(servers)})",
                    )
                )
            owner = name if isinstance(name, str) and name and name.isprintable() else f"resolver #{i + 1}"
            for j, s in enumerate(servers):
                errors += _server_errors(f"{at}.servers[{j}]", label, s, owner, servers_seen)
        enabled = r.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(ValidationError(f"{at}.enabled", "type", f"{label}: enabled must be true or false"))
        elif enabled:
            any_enabled = True
    if not any_enabled:
        errors.append(
            ValidationError("resolvers", "none_enabled", "resolvers: at least one resolver must be enabled")
        )
    return errors


def _server_errors(at: str, label: str, s: object, owner: str, seen: dict[str, str]) -> list[ValidationError]:
    """Problems with one server address. ``seen`` maps the addresses used so far to their resolver."""
    if isinstance(s, str) and not s.isprintable():
        return [
            ValidationError(at, "invalid", f"{label}: server {_short_repr(s)} contains control characters")
        ]
    try:
        ip = ipaddress.ip_address(s if isinstance(s, str) else "")
    except ValueError:
        return [
            ValidationError(
                at, "invalid", f"{label}: server {_short_repr(s)} is not a valid IPv4 or IPv6 address"
            )
        ]
    assert isinstance(s, str)  # ip_address("") failed for everything else
    problem = _server_problem(ip)
    if problem:
        shown = s if len(s) <= 64 else s[:61] + "..."
        text = (
            f"{label}: server {shown} {problem}"
            if problem.startswith("is ")
            else f"{label}: server {shown}: {problem}"
        )
        return [ValidationError(at, "unusable_address", text)]
    key = _host_key(ip)
    if key in seen:
        return [ValidationError(at, "duplicate", f"{label}: server {key} is already used by {seen[key]}")]
    seen[key] = owner
    return []


# Invalid domains listed one by one; the rest are counted in a single message.
MAX_DOMAIN_ERRORS = 20


def _validate_domains(domains: object) -> list[ValidationError]:
    if not isinstance(domains, list):
        return [ValidationError("domains", "type", "domains: must be a list of domain names")]
    errors: list[ValidationError] = []
    if not domains:
        errors.append(ValidationError("domains", "required", "domains: at least one domain is required"))
    elif len(domains) > MAX_DOMAINS:
        errors.append(
            ValidationError(
                "domains", "too_many", f"domains: at most {MAX_DOMAINS} domains allowed (got {len(domains)})"
            )
        )
    bad = 0
    for i, d in enumerate(domains):
        problem = _hostname_error(d)
        if problem:
            bad += 1
            if bad <= MAX_DOMAIN_ERRORS:
                errors.append(
                    ValidationError(
                        f"domains[{i}]", "invalid", f"domains: '{d}' is not a valid hostname: {problem}"
                    )
                )
    if bad > MAX_DOMAIN_ERRORS:
        errors.append(
            ValidationError(
                "domains", "more_errors", f"domains: ...and {bad - MAX_DOMAIN_ERRORS} more invalid domains"
            )
        )
    return errors


def _validate_settings(settings: object) -> list[ValidationError]:
    if not isinstance(settings, dict):
        return [ValidationError("settings", "type", "settings: must be an object")]
    errors: list[ValidationError] = []
    for key, (lo, hi) in SETTING_BOUNDS.items():
        v = settings.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            errors.append(
                ValidationError(
                    f"settings.{key}",
                    "out_of_range",
                    f"settings.{key}: must be a whole number from {lo} to {hi} (got {_short_repr(v)})",
                )
            )
    rt = settings.get("record_type")
    if rt not in RECORD_TYPES:
        errors.append(
            ValidationError(
                "settings.record_type",
                "invalid_choice",
                f"settings.record_type: must be one of {', '.join(RECORD_TYPES)} (got {_short_repr(rt)})",
            )
        )
    if not isinstance(settings.get("shuffle"), bool):
        errors.append(
            ValidationError(
                "settings.shuffle",
                "type",
                f"settings.shuffle: must be true or false (got {_short_repr(settings.get('shuffle'))})",
            )
        )
    return errors


def _validate_run_size(cfg: dict) -> list[ValidationError]:
    """The run as a whole, when the numbers it depends on are usable."""
    resolvers, domains, settings = cfg.get("resolvers"), cfg.get("domains"), cfg.get("settings")
    rounds = settings.get("rounds") if isinstance(settings, dict) else None
    if not (isinstance(resolvers, list) and isinstance(domains, list) and type(rounds) is int and rounds > 0):
        return []
    servers = sum(
        len(r["servers"])
        for r in resolvers
        if isinstance(r, dict) and r.get("enabled", True) is True and isinstance(r.get("servers"), list)
    )
    queries = servers * len(domains) * rounds
    if queries <= MAX_QUERIES_PER_RUN:
        return []
    return [
        ValidationError(
            "",
            "too_many_queries",
            f"a run would send {queries:,} queries ({servers} servers x {len(domains)} domains x "
            f"{rounds} rounds); the limit is {MAX_QUERIES_PER_RUN:,}, so enable fewer servers or "
            "use fewer domains or rounds",
        )
    ]


def validate_config(cfg: object) -> list[ValidationError]:
    """Validate a config. Returns its problems ([] = valid), section by section.

    The config is normalised first, so e.g. messy domain lists are judged
    after stripping/lower-casing/de-duplication, and paths point into the
    normalised config.
    """
    if not isinstance(cfg, dict):
        return [
            ValidationError("", "type", "Config must be a JSON object with resolvers, domains and settings")
        ]
    cfg = normalize_config(cfg)
    return (
        _validate_resolvers(cfg.get("resolvers"))
        + _validate_domains(cfg.get("domains"))
        + _validate_settings(cfg.get("settings"))
        + _validate_run_size(cfg)
    )


# --------------------------------------------------------------------------- #
# Writing and parsing
# --------------------------------------------------------------------------- #


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic write; any OSError becomes ConfigWriteError (clean CLI/API message)."""
    try:
        _atomic_write_text_raw(Path(path), text)
    except OSError as exc:
        why = exc.strerror or type(exc).__name__
        raise ConfigWriteError(
            ValidationError("", "write_failed", f"cannot write the file: {why}"), path
        ) from exc


def _atomic_write_text_raw(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def dumps_config(cfg: dict) -> str:
    return json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"


# A real config nests 4 levels (object > resolvers > resolver > servers). Python 3.13's parser gives up
# on absurd nesting by running out of recursion; 3.14's parses it, and validation then overflowed the
# stack formatting the value. Enforcing a limit here makes every Python reject the same input.
MAX_JSON_DEPTH = 32


def _nesting_exceeds(value: object, limit: int) -> bool:
    """True if ``value`` nests containers more than ``limit`` deep. Iterative, so it can't overflow."""
    stack = [(value, 1)] if isinstance(value, (dict, list)) else []
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            return True
        for child in node.values() if isinstance(node, dict) else node:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
    return False


def loads_json(text: str) -> object:
    """``json.loads`` for untrusted input: every failure is a ValueError, including nesting deeper than
    ``MAX_JSON_DEPTH`` (which Python 3.13 reports as RecursionError)."""
    too_deep = f"nested more than {MAX_JSON_DEPTH} levels deep"
    try:
        value = json.loads(text)
    except RecursionError:
        raise ValueError(too_deep) from None
    if _nesting_exceeds(value, MAX_JSON_DEPTH):
        raise ValueError(too_deep)
    return value


# --------------------------------------------------------------------------- #
# The "System" resolver
# --------------------------------------------------------------------------- #

Detect = Callable[[], sysdns.Detected]


@dataclass
class SystemResolver:
    """What to do with this computer's resolvers, given the other resolvers in a config."""

    resolver: dict | None  # the entry to add or update; None if there is nothing usable to add
    message: str  # one line for the user saying what was found and what happens
    detected: sysdns.Detected = field(default_factory=lambda: sysdns.Detected([]))


def _is_system(r: object) -> bool:
    return isinstance(r, dict) and isinstance(r.get("name"), str) and r["name"].strip().casefold() == "system"


def system_resolver(resolvers: object, detected: sysdns.Detected) -> SystemResolver:
    """The "System" entry for ``detected``, leaving out servers another resolver already has.

    ``resolvers`` may be anything a hand-edited config holds; entries that aren't resolvers are
    ignored. A server another resolver has would make the config invalid (each IP may appear once),
    and it would be measured twice anyway.
    """
    used: dict[str, str] = {}
    for r in resolvers if isinstance(resolvers, list) else []:
        if not isinstance(r, dict) or _is_system(r) or not isinstance(r.get("servers"), list):
            continue
        for s in r["servers"]:
            key = server_key(s) if isinstance(s, str) else None
            if key is not None:
                name = r.get("name")
                used.setdefault(key, name if isinstance(name, str) and name else "another resolver")
    keep: list[str] = []
    kept: set[str] = set()
    taken: dict[str, str] = {}
    extra: list[str] = []  # usable, but over the per-resolver limit
    for s in detected.servers:
        try:
            ip = ipaddress.ip_address(s)
        except ValueError:
            continue
        key = _host_key(ip)
        if _server_problem(ip) or key in kept:
            continue
        if key in used:
            taken[s] = used[key]
        elif len(keep) < MAX_SERVERS_PER_RESOLVER:
            keep.append(normalize_server(s))
            kept.add(key)
        else:
            extra.append(s)
    source = f" (from {detected.source})" if detected.source else ""
    if not detected.servers:
        return SystemResolver(None, f"No system resolvers found: {detected.describe_empty()}.", detected)
    if not keep:
        owners = sorted(set(taken.values()))
        return SystemResolver(
            None,
            f"This computer's resolvers ({', '.join(taken)}){source} are already in the list as "
            f"{', '.join(owners)}, so there is no System entry to add.",
            detected,
        )
    message = f"{SYSTEM_NAME}: {', '.join(keep)}{source}."
    if extra:
        message += f" Left out, over the limit of {MAX_SERVERS_PER_RESOLVER} servers: {', '.join(extra)}."
    if taken:
        message += (
            " Left out: " + ", ".join(f"{s} (already used by {name})" for s, name in taken.items()) + "."
        )
    return SystemResolver({"name": SYSTEM_NAME, "servers": keep, "enabled": True}, message, detected)


def with_system_resolver(cfg: dict, entry: dict) -> dict:
    """A copy of ``cfg`` whose "System" resolver has ``entry``'s servers (added at the end if missing).

    An existing entry keeps its name's spelling and its enabled flag: the user may have turned it off.
    """
    out = copy.deepcopy(cfg)
    resolvers = out.get("resolvers")
    if not isinstance(resolvers, list):
        out["resolvers"] = resolvers = []
    for r in resolvers:
        if _is_system(r):
            r["servers"] = list(entry["servers"])
            return out
    resolvers.append(copy.deepcopy(entry))
    return out


def initial_config(detect: Detect | None = None) -> tuple[dict, SystemResolver]:
    """The config a new config.json starts with: the defaults plus this computer's own resolvers."""
    cfg = default_config()
    system = system_resolver(cfg["resolvers"], (detect or sysdns.detect)())
    if system.resolver is not None:
        cfg = with_system_resolver(cfg, system.resolver)
    return cfg, system


# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #


def load_config(path: str | os.PathLike[str], strict: bool = True, detect: Detect | None = None) -> dict:
    """Load, normalise and (if ``strict``) validate the config at ``path``. Never writes.

    A missing file loads as ``initial_config()``, without creating it. Invalid JSON raises
    ConfigError. With ``strict=False`` a structurally-invalid (but parseable) config is returned
    anyway so the UI can show and fix it.
    """
    path = Path(path)
    if not path.exists():
        return initial_config(detect)[0]
    try:
        raw = loads_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        why = exc.strerror or type(exc).__name__
        raise ConfigError(ValidationError("", "unreadable", f"cannot read the file: {why}"), path) from exc
    except ValueError as exc:
        # JSONDecodeError, a >4300-digit integer literal, absurdly deep nesting, bad UTF-8
        msg = str(exc)
        msg = msg if len(msg) <= 200 else msg[:197] + "..."
        raise ConfigError(ValidationError("", "invalid_json", f"not valid JSON: {msg}"), path) from exc
    if not isinstance(raw, dict):
        raise ConfigError(ValidationError("", "type", "must contain a JSON object"), path)
    cfg = normalize_config(raw)
    if strict:
        errors = validate_config(cfg)
        if errors:
            raise ConfigError(errors, path)
    return cfg


def ensure_config(path: str | os.PathLike[str], detect: Detect | None = None) -> SystemResolver | None:
    """Create the config at ``path`` from ``initial_config()`` if it doesn't exist yet.

    Returns what system-resolver detection found for the new file, or None if the file was already
    there. Called when a run or the web UI starts, so every run of a new config uses the same System
    entry instead of detecting it afresh.
    """
    path = Path(path)
    if path.exists():
        return None
    cfg, system = initial_config(detect)
    _atomic_write_text(path, dumps_config(cfg))
    return system


def save_config(cfg: dict, path: str | os.PathLike[str]) -> dict:
    """Normalise + validate, then atomically write. Returns the saved config."""
    norm = normalize_config(cfg)
    errors = validate_config(norm)
    if errors:
        raise ConfigError(errors)
    _atomic_write_text(Path(path), dumps_config(norm))
    return norm


def reset_config(path: str | os.PathLike[str], detect: Detect | None = None) -> tuple[dict, SystemResolver]:
    """Overwrite the config at ``path`` with ``initial_config()``; returns it and what detection found."""
    cfg, system = initial_config(detect)
    _atomic_write_text(Path(path), dumps_config(cfg))
    return cfg, system


def enabled_resolvers(cfg: dict) -> list[dict]:
    return [r for r in cfg.get("resolvers", []) if r.get("enabled", True)]


def current_resolver_names(path: str | os.PathLike[str]) -> list[str] | None:
    """Names of the resolvers enabled in the config at ``path``, read-only.

    None if the file is missing or unreadable (callers then fall back to the
    newest run's config). Never creates or rewrites the file.
    """
    path = Path(path)
    try:
        if not path.is_file():
            return None
        cfg = load_config(path, strict=False)
    except (ConfigError, OSError):
        return None
    resolvers = cfg.get("resolvers") if isinstance(cfg, dict) else None
    if not isinstance(resolvers, list):
        return None
    return [
        r["name"]
        for r in resolvers
        if isinstance(r, dict) and isinstance(r.get("name"), str) and r.get("enabled", True) is not False
    ]


# Average and worst-case slack the runner adds to the interval (runner.JITTER is up to 10 %).
_AVG_JITTER, _MAX_JITTER = 1.05, 1.10


def estimate(cfg: object, rounds: int | None = None) -> dict:
    """Rough cost of a run of ``cfg`` (with ``rounds`` instead of its own, if given). Never raises.

    Works on any config, even an invalid draft: a setting that isn't a usable number counts as its
    default, so the Settings page can show an estimate while the user is still typing.
    ``est_seconds`` assumes every query is answered at once; ``worst_seconds`` assumes every attempt
    times out.
    """
    cfg = normalize_config(cfg) if isinstance(cfg, dict) else {}
    settings = cfg.get("settings")
    raw = settings if isinstance(settings, dict) else {}

    def setting(key: str) -> int:
        v = raw.get(key)
        return v if type(v) is int and v > 0 else DEFAULT_SETTINGS[key]  # type: ignore[return-value] # the int defaults

    resolvers = cfg.get("resolvers")
    enabled = [
        r
        for r in (resolvers if isinstance(resolvers, list) else [])
        if isinstance(r, dict) and r.get("enabled", True) is not False and isinstance(r.get("servers"), list)
    ]
    servers = sum(len(r["servers"]) for r in enabled)
    domains = cfg.get("domains")
    n_domains = len(domains) if isinstance(domains, list) else 0
    n_rounds = rounds if rounds is not None else setting("rounds")
    per_server = n_domains * n_rounds
    interval_s = max(MIN_INTERVAL_MS, setting("per_server_interval_ms")) / 1000.0  # the runner's hard floor
    timeout_s = setting("timeout_ms") / 1000.0
    per_server_qps = 1.0 / interval_s
    return {
        "resolvers": len(enabled),
        "servers": servers,
        "domains": n_domains,
        "rounds": n_rounds,
        "queries": servers * per_server,
        "queries_per_server": per_server,
        # every server is measured at the same time, each at its own pace
        "est_seconds": round(per_server * interval_s * _AVG_JITTER, 1) if servers else 0.0,
        # each attempt waits for its slot and then, at worst, for the whole timeout
        "worst_seconds": round(per_server * setting("tries") * max(interval_s * _MAX_JITTER, timeout_s), 1)
        if servers
        else 0.0,
        "max_qps_per_server": round(per_server_qps, 2),
        "max_qps_total": round(per_server_qps * servers, 2),
    }
