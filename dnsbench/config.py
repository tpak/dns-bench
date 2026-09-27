"""Configuration: defaults, normalisation, validation, atomic load/save.

The config file is plain JSON (see README.md for the schema). The web UI
overwrites it on save, so everything that reaches disk goes through
``normalize_config`` + ``validate_config`` first.
"""

from __future__ import annotations

import contextlib
import copy
import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
WEB_DIR = PACKAGE_DIR / "web"
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.json"
DEFAULT_RUNS_DIR = PROJECT_DIR / "runs"

# The 60 domains from the original dns-test.sh, in the same order.
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

# Quad9 is disabled because the original script defined it but left it out of `order`.
DEFAULT_RESOLVERS = [
    {"name": "OpenDNS", "servers": ["208.67.222.222", "208.67.220.220"], "enabled": True},
    {"name": "Cloudflare", "servers": ["1.1.1.1", "1.0.0.1"], "enabled": True},
    {"name": "Google", "servers": ["8.8.8.8", "8.8.4.4"], "enabled": True},
    {"name": "Quad9", "servers": ["9.9.9.9", "149.112.112.112"], "enabled": False},
    {"name": "ISP", "servers": ["61.9.134.49", "61.9.133.193"], "enabled": True},
]

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

MAX_DOMAINS = 500
MAX_RESOLVERS = 20
MAX_SERVERS_PER_RESOLVER = 4
MAX_NAME_LEN = 40
MAX_HOSTNAME_LEN = 253  # a DNS name in text form, without the trailing dot (RFC 1035)
# Servers x domains x rounds in one run (the defaults send 480). It bounds a run's duration, its file
# size (a few hundred bytes per query) and what the web UI has to draw.
MAX_QUERIES_PER_RUN = 50_000

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SPLIT_RE = re.compile(r"[\s,;]+")
# ASCII digits only (``str.isdigit`` also accepts e.g. "²", which int() rejects);
# 9 digits is far beyond every bound and far below int()'s 4300-digit limit.
_INT_RE = re.compile(r"-?[0-9]{1,9}")
# IPv6 zone IDs are only accepted on link-local addresses, as an interface name.
_ZONE_RE = re.compile(r"[A-Za-z0-9._-]{1,15}")


class ConfigError(Exception):
    """Raised for unreadable or invalid configuration.

    ``errors`` is always a list of human-readable strings.
    """

    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) if self.errors else "Invalid config")


class ConfigWriteError(ConfigError):
    """The config could not be written (permissions, full disk, ...).

    A subclass so the CLI reports it like any other config problem, while the
    HTTP API can tell a disk failure (500) apart from invalid input (400).
    """


def default_config() -> dict:
    """Return a fresh deep copy of the default configuration."""
    return copy.deepcopy(DEFAULT_CONFIG)


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


def validate_config(cfg) -> list[str]:
    """Validate a config. Returns a list of human-readable errors ([] = valid).

    The config is normalised first, so e.g. messy domain lists are judged
    after stripping/lower-casing/de-duplication.
    """
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return ["Config must be a JSON object with resolvers, domains and settings"]
    cfg = normalize_config(cfg)

    # -- resolvers ---------------------------------------------------------
    resolvers = cfg.get("resolvers")
    if not isinstance(resolvers, list) or not resolvers:
        errors.append("resolvers: at least one resolver is required")
    else:
        if len(resolvers) > MAX_RESOLVERS:
            errors.append(f"resolvers: at most {MAX_RESOLVERS} resolvers allowed (got {len(resolvers)})")
        names_seen: dict[str, int] = {}
        servers_seen: dict[str, str] = {}
        any_enabled = False
        for i, r in enumerate(resolvers, 1):
            label = f"Resolver #{i}"
            if not isinstance(r, dict):
                errors.append(f"{label}: must be an object with name, servers, enabled")
                continue
            name = r.get("name")
            if not isinstance(name, str) or not name:
                errors.append(f"{label}: name is required")
            else:
                # never echo control characters (terminal escapes) back in messages
                label = f"Resolver #{i} ({name if name.isprintable() else _short_repr(name)})"
                if len(name) > MAX_NAME_LEN:
                    errors.append(f"{label}: name must be at most {MAX_NAME_LEN} characters")
                # isprintable() is False for C0/DEL/C1 controls and bidi overrides
                if "," in name or not name.isprintable():
                    errors.append(f"{label}: name must not contain commas or control characters")
                key = name.casefold()
                if key in names_seen:
                    errors.append(f"{label}: duplicate name (same as resolver #{names_seen[key]})")
                else:
                    names_seen[key] = i
            servers = r.get("servers")
            if not isinstance(servers, list) or not servers:
                errors.append(f"{label}: at least one server IP is required")
            else:
                if len(servers) > MAX_SERVERS_PER_RESOLVER:
                    errors.append(
                        f"{label}: at most {MAX_SERVERS_PER_RESOLVER} servers allowed (got {len(servers)})"
                    )
                for s in servers:
                    if isinstance(s, str) and not s.isprintable():
                        errors.append(f"{label}: server {_short_repr(s)} contains control characters")
                        continue
                    try:
                        ip = ipaddress.ip_address(s if isinstance(s, str) else "")
                    except ValueError:
                        errors.append(f"{label}: server {_short_repr(s)} is not a valid IPv4 or IPv6 address")
                        continue
                    problem = _server_problem(ip)
                    if problem:
                        shown = s if len(s) <= 64 else s[:61] + "..."
                        errors.append(
                            f"{label}: server {shown} {problem}"
                            if problem.startswith("is ")
                            else f"{label}: server {shown}: {problem}"
                        )
                        continue
                    key = _host_key(ip)
                    if key in servers_seen:
                        errors.append(f"{label}: server {key} is already used by {servers_seen[key]}")
                    else:
                        servers_seen[key] = (
                            name
                            if isinstance(name, str) and name and name.isprintable()
                            else f"resolver #{i}"
                        )
            enabled = r.get("enabled", True)
            if not isinstance(enabled, bool):
                errors.append(f"{label}: enabled must be true or false")
            elif enabled:
                any_enabled = True
        if not any_enabled:
            errors.append("resolvers: at least one resolver must be enabled")

    # -- domains -----------------------------------------------------------
    domains = cfg.get("domains")
    if not isinstance(domains, list):
        errors.append("domains: must be a list of domain names")
    else:
        if not domains:
            errors.append("domains: at least one domain is required")
        elif len(domains) > MAX_DOMAINS:
            errors.append(f"domains: at most {MAX_DOMAINS} domains allowed (got {len(domains)})")
        bad = 0
        for d in domains:
            problem = _hostname_error(d)
            if problem:
                bad += 1
                if bad <= 20:
                    errors.append(f"domains: '{d}' is not a valid hostname: {problem}")
        if bad > 20:
            errors.append(f"domains: ...and {bad - 20} more invalid domains")

    # -- settings ----------------------------------------------------------
    settings = cfg.get("settings")
    if not isinstance(settings, dict):
        errors.append("settings: must be an object")
    else:
        for key, (lo, hi) in SETTING_BOUNDS.items():
            v = settings.get(key)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                errors.append(
                    f"settings.{key}: must be a whole number from {lo} to {hi} (got {_short_repr(v)})"
                )
        rt = settings.get("record_type")
        if rt not in RECORD_TYPES:
            errors.append(
                f"settings.record_type: must be one of {', '.join(RECORD_TYPES)} (got {_short_repr(rt)})"
            )
        if not isinstance(settings.get("shuffle"), bool):
            errors.append(
                f"settings.shuffle: must be true or false (got {_short_repr(settings.get('shuffle'))})"
            )

    # -- the run as a whole, when the numbers it depends on are usable ------
    rounds = settings.get("rounds") if isinstance(settings, dict) else None
    if isinstance(resolvers, list) and isinstance(domains, list) and type(rounds) is int and rounds > 0:
        servers = sum(
            len(r["servers"])
            for r in resolvers
            if isinstance(r, dict) and r.get("enabled", True) is True and isinstance(r.get("servers"), list)
        )
        queries = servers * len(domains) * rounds
        if queries > MAX_QUERIES_PER_RUN:
            errors.append(
                f"a run would send {queries:,} queries ({servers} servers x {len(domains)} domains x "
                f"{rounds} rounds); the limit is {MAX_QUERIES_PER_RUN:,}, so enable fewer servers or "
                "use fewer domains or rounds"
            )
    return errors


# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic write; any OSError becomes ConfigWriteError (clean CLI/API message)."""
    try:
        _atomic_write_text_raw(Path(path), text)
    except OSError as exc:
        raise ConfigWriteError(f"cannot write {path}: {exc.strerror or exc}") from exc


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


def load_config(path=DEFAULT_CONFIG_PATH, strict: bool = True) -> dict:
    """Load, normalise and (if ``strict``) validate the config at ``path``.

    A missing file is created with the defaults. Invalid JSON raises
    ConfigError. With ``strict=False`` a structurally-invalid (but parseable)
    config is returned anyway so the UI can show and fix it.
    """
    path = Path(path)
    if not path.exists():
        cfg = default_config()
        _atomic_write_text(path, dumps_config(cfg))
        return cfg
    try:
        raw = loads_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    except ValueError as exc:
        # JSONDecodeError, a >4300-digit integer literal, absurdly deep nesting, bad UTF-8
        msg = str(exc)
        msg = msg if len(msg) <= 200 else msg[:197] + "..."
        raise ConfigError(f"{path} is not valid JSON: {msg}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    cfg = normalize_config(raw)
    if strict:
        errors = validate_config(cfg)
        if errors:
            raise ConfigError([f"{path}: {e}" for e in errors])
    return cfg


def save_config(cfg: dict, path=DEFAULT_CONFIG_PATH) -> dict:
    """Normalise + validate, then atomically write. Returns the saved config."""
    norm = normalize_config(cfg)
    errors = validate_config(norm)
    if errors:
        raise ConfigError(errors)
    _atomic_write_text(Path(path), dumps_config(norm))
    return norm


def reset_config(path=DEFAULT_CONFIG_PATH) -> dict:
    cfg = default_config()
    _atomic_write_text(Path(path), dumps_config(cfg))
    return cfg


def enabled_resolvers(cfg: dict) -> list[dict]:
    return [r for r in cfg.get("resolvers", []) if r.get("enabled", True)]


def current_resolver_names(path=DEFAULT_CONFIG_PATH) -> list[str] | None:
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


def estimate(cfg: dict) -> dict:
    """Rough cost of a run: total queries, expected seconds, max load."""
    s = {**DEFAULT_SETTINGS, **(cfg.get("settings") or {})}
    servers = sum(len(r.get("servers", [])) for r in enabled_resolvers(cfg))
    per_server = len(cfg.get("domains", [])) * int(s["rounds"])
    interval = max(MIN_INTERVAL_MS, int(s["per_server_interval_ms"])) / 1000.0
    per_server_qps = 1.0 / interval
    return {
        "servers": servers,
        "queries": servers * per_server,
        "queries_per_server": per_server,
        # every server is measured at the same time; 5 % average jitter on top of the interval
        "est_seconds": round(per_server * interval * 1.05, 1) if servers else 0.0,
        "max_qps_per_server": round(per_server_qps, 2),
        "max_qps_total": round(per_server_qps * servers, 2),
    }
