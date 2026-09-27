from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path

from dnsbench import config as C


def cfg(**changes):
    c = C.default_config()
    for k, v in changes.items():
        c[k] = v
    return c


class LoadsJsonTest(unittest.TestCase):
    def test_accepts_nesting_up_to_the_limit(self):
        depth = C.MAX_JSON_DEPTH
        value = C.loads_json("[" * depth + "]" * depth)
        for _ in range(depth - 1):
            (value,) = value
        self.assertEqual(value, [])

    def test_rejects_nesting_past_the_limit_on_every_python(self):
        for depth in (C.MAX_JSON_DEPTH + 1, 100000):
            for text in ("[" * depth + "]" * depth, '{"a": ' + "[" * (depth - 1) + "]" * (depth - 1) + "}"):
                with self.subTest(depth=depth, text=text[:6]), self.assertRaises(ValueError) as cm:
                    C.loads_json(text)
                self.assertEqual(str(cm.exception), f"nested more than {C.MAX_JSON_DEPTH} levels deep")

    def test_every_parse_failure_is_a_value_error(self):
        for text in ("{ nope", "9" * 5000, ""):
            with self.subTest(text=text[:10]), self.assertRaises(ValueError):
                C.loads_json(text)

    def test_a_real_config_is_well_inside_the_limit(self):
        self.assertEqual(C.loads_json(C.dumps_config(C.default_config())), C.default_config())


class DefaultsTest(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(C.validate_config(C.default_config()), [])

    def test_default_domains_match_spec(self):
        self.assertEqual(len(C.DEFAULT_DOMAINS), 60)
        self.assertEqual(C.DEFAULT_DOMAINS[0], "google.com")
        self.assertEqual(C.DEFAULT_DOMAINS[-1], "realestate.com.au")

    def test_default_domains_match_original_script(self):
        # The script is tracked in archive/, so a missing file is a failure, not a skip: this test was
        # skipped without anyone noticing after the script moved there.
        original = Path(__file__).resolve().parents[1] / "archive" / "dns-test.sh"
        text = original.read_text(encoding="utf-8")
        block = re.search(r"domains=\((.*?)\)", text, re.S).group(1)
        self.assertEqual(block.split(), C.DEFAULT_DOMAINS)

    def test_default_resolvers(self):
        names = [r["name"] for r in C.DEFAULT_RESOLVERS]
        self.assertEqual(names, ["OpenDNS", "Cloudflare", "Google", "Quad9", "ISP"])
        quad9 = next(r for r in C.DEFAULT_RESOLVERS if r["name"] == "Quad9")
        self.assertFalse(quad9["enabled"])
        self.assertEqual(C.DEFAULT_SETTINGS["per_server_interval_ms"], 250)

    def test_default_config_is_a_copy(self):
        a = C.default_config()
        a["domains"].append("example.com")
        self.assertNotIn("example.com", C.default_config()["domains"])

    def test_shipped_config_json_matches_defaults(self):
        shipped = Path(__file__).resolve().parents[1] / "config.json"
        if not shipped.exists():
            self.skipTest("config.json not present")
        self.assertEqual(C.validate_config(json.loads(shipped.read_text())), [])


class NormalizeTest(unittest.TestCase):
    def test_domains_normalised(self):
        c = cfg(domains=["  Example.COM. ", "", "example.com", "b.org", "B.org.", "   "])
        n = C.normalize_config(c)
        self.assertEqual(n["domains"], ["example.com", "b.org"])

    def test_domains_from_string(self):
        n = C.normalize_config(cfg(domains="a.com\nb.com, c.com  d.com"))
        self.assertEqual(n["domains"], ["a.com", "b.com", "c.com", "d.com"])

    def test_idna(self):
        n = C.normalize_config(cfg(domains=["bücher.de"]))
        self.assertEqual(n["domains"], ["xn--bcher-kva.de"])
        self.assertEqual(C.validate_config(n), [])

    def test_overlong_unicode_names_are_not_idna_encoded(self):
        # Encoding work grows with the input, which can be a whole 1 MB request body; such a name can
        # never be valid, so it's left as-is for validation to reject.
        huge = "é" * 1_000_000 + ".com"
        t0 = time.perf_counter()
        n = C.normalize_config(cfg(domains=[huge]))
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(n["domains"], [huge])
        errors = C.validate_config(n)
        self.assertTrue(any("longer than 253 characters" in e for e in errors), errors)
        long_but_valid = ".".join(["bücher" + "a" * 50] * 3)  # 170 characters: still encoded
        encoded = C.normalize_domain(long_but_valid)
        self.assertTrue(encoded.startswith("xn--"), encoded)
        self.assertEqual(C.validate_config(cfg(domains=[encoded])), [])

    def test_servers_split_and_canonicalised(self):
        c = cfg(resolvers=[{"name": " X ", "servers": "1.1.1.1, 2001:0db8:0000::0001"}])
        n = C.normalize_config(c)
        self.assertEqual(n["resolvers"][0]["name"], "X")
        self.assertEqual(n["resolvers"][0]["servers"], ["1.1.1.1", "2001:db8::1"])
        self.assertTrue(n["resolvers"][0]["enabled"])  # defaulted

    def test_ipv4_mapped_folded_to_ipv4(self):
        self.assertEqual(C.normalize_server("::ffff:8.8.8.8"), "8.8.8.8")
        self.assertEqual(C.normalize_server("[::ffff:8.8.8.8]"), "8.8.8.8")
        self.assertEqual(C.normalize_server("fe80::1%en0"), "fe80::1%en0")  # zone kept for validation
        self.assertEqual(C.normalize_server("junk"), "junk")

    def test_server_key(self):
        self.assertEqual(C.server_key("::ffff:1.1.1.1%3"), "1.1.1.1")
        self.assertEqual(C.server_key("::1%7"), "::1")
        self.assertEqual(C.server_key("2001:0db8::0001"), "2001:db8::1")
        self.assertIsNone(C.server_key("dns.google"))

    def test_missing_settings_filled(self):
        c = cfg(settings={"rounds": 3})
        n = C.normalize_config(c)
        self.assertEqual(n["settings"]["rounds"], 3)
        self.assertEqual(n["settings"]["timeout_ms"], 1000)
        self.assertEqual(set(n["settings"]), set(C.DEFAULT_SETTINGS))

    def test_settings_coercion(self):
        n = C.normalize_config(cfg(settings={"rounds": 2.0, "timeout_ms": "1500", "record_type": "aaaa"}))
        self.assertEqual(n["settings"]["rounds"], 2)
        self.assertEqual(n["settings"]["timeout_ms"], 1500)
        self.assertEqual(n["settings"]["record_type"], "AAAA")
        self.assertEqual(C.validate_config(n), [])

    def test_unknown_keys_dropped(self):
        c = cfg()
        c["junk"] = 1
        c["settings"]["junk"] = 2
        n = C.normalize_config(c)
        self.assertNotIn("junk", n)
        self.assertNotIn("junk", n["settings"])


class ValidateTest(unittest.TestCase):
    def assertInvalid(self, c, fragment):
        errors = C.validate_config(c)
        self.assertTrue(errors, "expected errors")
        self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")

    def test_not_a_dict(self):
        self.assertTrue(C.validate_config([]))

    def test_no_resolvers(self):
        self.assertInvalid(cfg(resolvers=[]), "at least one resolver")

    def test_at_most_max_resolvers(self):
        many = [{"name": f"R{i}", "servers": [f"192.0.2.{i}"], "enabled": i == 1} for i in range(1, 22)]
        self.assertInvalid(cfg(resolvers=many), f"at most {C.MAX_RESOLVERS} resolvers allowed (got 21)")
        self.assertEqual(C.validate_config(cfg(resolvers=many[: C.MAX_RESOLVERS])), [])

    def test_queries_per_run_are_capped(self):
        def resolvers(servers, disabled=0):
            ips = [f"192.0.2.{i}" for i in range(1, servers + disabled + 1)]
            return [{"name": f"R{i}", "servers": [ip], "enabled": i < servers} for i, ip in enumerate(ips)]

        domains = [f"d{i}.example" for i in range(500)]
        at_limit = cfg(resolvers=resolvers(10), domains=domains)
        at_limit["settings"]["rounds"] = 10
        self.assertEqual(C.validate_config(at_limit), [])  # 10 x 500 x 10 = 50,000
        # disabled resolvers send nothing, so they don't count
        with_disabled = cfg(resolvers=resolvers(10, disabled=5), domains=domains)
        with_disabled["settings"]["rounds"] = 10
        self.assertEqual(C.validate_config(with_disabled), [])
        over = cfg(resolvers=resolvers(11), domains=domains)
        over["settings"]["rounds"] = 10
        self.assertInvalid(over, "a run would send 55,000 queries (11 servers x 500 domains x 10 rounds)")
        # reported alongside unrelated problems, not hidden behind them
        over["settings"]["shuffle"] = "yes"
        self.assertEqual(len(C.validate_config(over)), 2)

    def test_name_required(self):
        self.assertInvalid(cfg(resolvers=[{"name": " ", "servers": ["1.1.1.1"]}]), "name is required")

    def test_name_too_long(self):
        self.assertInvalid(cfg(resolvers=[{"name": "x" * 41, "servers": ["1.1.1.1"]}]), "at most 40")

    def test_name_with_comma(self):
        self.assertInvalid(cfg(resolvers=[{"name": "a,b", "servers": ["1.1.1.1"]}]), "commas")

    def test_duplicate_names_case_insensitive(self):
        self.assertInvalid(
            cfg(
                resolvers=[
                    {"name": "Google", "servers": ["8.8.8.8"]},
                    {"name": "google", "servers": ["8.8.4.4"]},
                ]
            ),
            "duplicate name",
        )

    def test_bad_ip(self):
        self.assertInvalid(cfg(resolvers=[{"name": "X", "servers": ["1.2.3"]}]), "not a valid IPv4 or IPv6")

    def test_hostname_not_ip(self):
        self.assertInvalid(cfg(resolvers=[{"name": "X", "servers": ["dns.google"]}]), "not a valid")

    def test_ipv6_ok(self):
        self.assertEqual(
            C.validate_config(cfg(resolvers=[{"name": "X", "servers": ["2606:4700:4700::1111"]}])), []
        )

    def test_server_count(self):
        self.assertInvalid(cfg(resolvers=[{"name": "X", "servers": []}]), "at least one server")
        self.assertInvalid(
            cfg(
                resolvers=[{"name": "X", "servers": ["1.1.1.1", "1.1.1.2", "1.1.1.3", "1.1.1.4", "1.1.1.5"]}]
            ),
            "at most 4",
        )

    def test_duplicate_ip_across_resolvers(self):
        self.assertInvalid(
            cfg(resolvers=[{"name": "A", "servers": ["1.1.1.1"]}, {"name": "B", "servers": ["1.1.1.1"]}]),
            "already used",
        )

    def test_duplicate_ipv6_different_spelling(self):
        self.assertInvalid(
            cfg(
                resolvers=[
                    {"name": "A", "servers": ["2001:db8::1"]},
                    {"name": "B", "servers": ["2001:0db8:0:0::1"]},
                ]
            ),
            "already used",
        )

    def test_duplicate_ipv4_mapped(self):
        self.assertInvalid(
            cfg(
                resolvers=[
                    {"name": "A", "servers": ["1.1.1.1"]},
                    {"name": "B", "servers": ["::ffff:1.1.1.1"]},
                ]
            ),
            "server 1.1.1.1 is already used by A",
        )

    def test_zone_ids(self):
        evil = "::ffff:127.0.0.1%\x1b]0;PWNED\x07\x1b[41mX\x1b[0m\nFAKE LINE: Recommendation: use EvilDNS"
        errors = C.validate_config(cfg(resolvers=[{"name": "A", "servers": [evil]}]))
        self.assertTrue(any("control characters" in e for e in errors), errors)
        self.assertFalse(any("\x1b" in e or "\n" in e for e in errors), errors)  # never echoed raw
        for bad in ("::ffff:1.2.3.4%x", "2001:db8::1%en0", "::1%1", "fe80::1%a,b", "fe80::1%" + "e" * 16):
            with self.subTest(bad=bad):
                self.assertInvalid(cfg(resolvers=[{"name": "A", "servers": [bad]}]), "zone ID")
        for good in ("fe80::1%en0", "fe80::1%eth0.100", "fe80::1%enp0s31f6"):
            with self.subTest(good=good):
                self.assertEqual(C.validate_config(cfg(resolvers=[{"name": "A", "servers": [good]}])), [])
        self.assertInvalid(
            cfg(
                resolvers=[
                    {"name": "A", "servers": ["fe80::1%en0"]},
                    {"name": "B", "servers": ["fe80::1%en1"]},
                ]
            ),
            "already used",
        )

    def test_non_unicast_servers_rejected(self):
        for bad in (
            "0.0.0.0",
            "::",
            "255.255.255.255",
            "240.0.0.1",
            "224.0.0.251",
            "ff02::1",
            "::ffff:224.0.0.1",
            "239.255.255.250",
        ):
            with self.subTest(bad=bad):
                self.assertInvalid(cfg(resolvers=[{"name": "A", "servers": [bad]}]), "not a DNS resolver")
        for good in (
            "127.0.0.1",
            "::1",
            "fe80::1",
            "192.168.1.1",
            "64:ff9b::808:808",
            "2001:db8::1",
            "10.0.0.1",
        ):
            with self.subTest(good=good):
                self.assertEqual(C.validate_config(cfg(resolvers=[{"name": "A", "servers": [good]}])), [])

    def test_name_with_invisible_controls(self):
        for bad in ("a\x9bb", "a\u202eb", "a\x7fb", "a\u2066b"):
            with self.subTest(bad=bad):
                errors = C.validate_config(cfg(resolvers=[{"name": bad, "servers": ["1.1.1.1"]}]))
                self.assertTrue(any("control characters" in e for e in errors), errors)
                self.assertFalse(any(bad in e for e in errors), errors)  # shown escaped
        self.assertEqual(
            C.validate_config(cfg(resolvers=[{"name": "My DNS (home)", "servers": ["1.1.1.1"]}])), []
        )

    def test_unconvertible_numeric_strings(self):
        for bad in ("--5", "\u00b2", "9" * 5000, "1e3", "+-1"):
            with self.subTest(bad=repr(bad)[:20]):
                n = C.normalize_config(cfg(settings={"rounds": bad}))  # never raises
                self.assertEqual(n["settings"]["rounds"], bad)
                errors = C.validate_config(n)
                self.assertTrue(any(e.startswith("settings.rounds") for e in errors), errors)
                self.assertTrue(all(len(e) < 200 for e in errors))

    def test_enabled_must_be_bool(self):
        self.assertInvalid(
            cfg(resolvers=[{"name": "A", "servers": ["1.1.1.1"], "enabled": "yes"}]),
            "enabled must be true or false",
        )

    def test_one_enabled(self):
        self.assertInvalid(
            cfg(resolvers=[{"name": "A", "servers": ["1.1.1.1"], "enabled": False}]),
            "at least one resolver must be enabled",
        )

    def test_domains_required(self):
        self.assertInvalid(cfg(domains=[]), "at least one domain")
        self.assertInvalid(cfg(domains=["", " "]), "at least one domain")

    def test_too_many_domains(self):
        self.assertInvalid(cfg(domains=[f"d{i}.com" for i in range(501)]), "at most 500")
        self.assertEqual(C.validate_config(cfg(domains=[f"d{i}.com" for i in range(500)])), [])

    def test_bad_hostnames(self):
        for bad in [
            "-a.com",
            "a-.com",
            "a..com",
            "a_b.com",
            "a b.com",
            "x" * 64 + ".com",
            "a\n.com",
            ".".join(["a" * 60] * 5),
        ]:
            with self.subTest(bad=bad):
                self.assertInvalid(cfg(domains=[bad]), "not a valid hostname")

    def test_good_hostnames(self):
        for good in [
            "a.com",
            "xn--bcher-kva.de",
            "a-b.co.uk",
            "localhost",
            "1.2.3.4.in-addr.arpa",
            "x" * 63 + ".com",
        ]:
            with self.subTest(good=good):
                self.assertEqual(C.validate_config(cfg(domains=[good])), [])

    def test_setting_bounds(self):
        for key, (lo, hi) in C.SETTING_BOUNDS.items():
            for bad in (lo - 1, hi + 1, "abc", None, True, 1.5):
                with self.subTest(key=key, value=bad):
                    c = cfg()
                    c["settings"][key] = bad
                    self.assertInvalid(c, f"settings.{key}")
            for good in (lo, hi):
                c = cfg()
                c["settings"][key] = good
                self.assertEqual(C.validate_config(c), [], (key, good))

    def test_interval_hard_floor(self):
        c = cfg()
        c["settings"]["per_server_interval_ms"] = 49
        self.assertInvalid(c, "per_server_interval_ms")

    def test_record_type_and_shuffle(self):
        c = cfg()
        c["settings"]["record_type"] = "MX"
        self.assertInvalid(c, "record_type")
        c = cfg()
        c["settings"]["shuffle"] = "yes"
        self.assertInvalid(c, "shuffle")


class LoadSaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sub" / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_writes_defaults(self):
        c = C.load_config(self.path)
        self.assertEqual(c, C.default_config())
        self.assertTrue(self.path.exists())
        self.assertEqual(json.loads(self.path.read_text()), C.default_config())

    def test_invalid_json(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ nope")
        with self.assertRaises(C.ConfigError) as cm:
            C.load_config(self.path)
        self.assertIn("not valid JSON", str(cm.exception))

    def test_unparseable_json_values(self):
        self.path.parent.mkdir(parents=True)
        for text in ('{"settings": {"rounds": ' + "9" * 5000 + "}}", "[" * 100000 + "]" * 100000):
            with self.subTest(text=text[:20]):
                self.path.write_text(text)
                with self.assertRaises(C.ConfigError) as cm:
                    C.load_config(self.path)
                self.assertIn("not valid JSON", str(cm.exception))

    def test_deep_nesting_inside_the_object_is_not_valid_json(self):
        # Python 3.14's parser accepts 100,000-deep nesting that 3.13's rejects; validation then
        # overflowed the stack formatting the value. A modest depth pins the limit on every version.
        self.path.parent.mkdir(parents=True)
        for depth in (C.MAX_JSON_DEPTH, 100000):
            nested = "[" * depth + "]" * depth
            for text in ('{"settings": {"rounds": ' + nested + "}}", '{"domains": ' + nested + "}"):
                with self.subTest(depth=depth, text=text[:14]):
                    self.path.write_text(text)
                    for strict in (True, False):
                        with self.assertRaises(C.ConfigError) as cm:
                            C.load_config(self.path, strict=strict)
                        self.assertIn(f"nested more than {C.MAX_JSON_DEPTH} levels", str(cm.exception))

    def test_bad_numeric_string_in_file(self):
        self.path.parent.mkdir(parents=True)
        bad = C.default_config()
        bad["settings"]["rounds"] = "--5"
        self.path.write_text(json.dumps(bad))
        with self.assertRaises(C.ConfigError):
            C.load_config(self.path)
        self.assertEqual(C.load_config(self.path, strict=False)["settings"]["rounds"], "--5")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unwritable_location_raises_config_write_error(self):
        ro = Path(self.tmp.name) / "ro"
        ro.mkdir()
        ro.chmod(0o555)
        try:
            with self.assertRaises(C.ConfigWriteError) as cm:
                C.reset_config(ro / "c.json")
            self.assertIn("cannot write", str(cm.exception))
            self.assertIsInstance(cm.exception, C.ConfigError)
            with self.assertRaises(C.ConfigWriteError):
                C.load_config(ro / "missing.json")
            with self.assertRaises(C.ConfigWriteError):
                C.save_config(C.default_config(), ro / "c.json")
            self.assertEqual(list(ro.iterdir()), [])  # no temp files left behind
        finally:
            ro.chmod(0o755)

    def test_invalid_config_strict_and_lenient(self):
        self.path.parent.mkdir(parents=True)
        bad = C.default_config()
        bad["settings"]["rounds"] = 99
        self.path.write_text(json.dumps(bad))
        with self.assertRaises(C.ConfigError) as cm:
            C.load_config(self.path)
        self.assertTrue(cm.exception.errors)
        lenient = C.load_config(self.path, strict=False)
        self.assertEqual(lenient["settings"]["rounds"], 99)

    def test_missing_settings_keys_filled_on_load(self):
        self.path.parent.mkdir(parents=True)
        c = C.default_config()
        del c["settings"]["shuffle"]
        del c["settings"]["timeout_ms"]
        self.path.write_text(json.dumps(c))
        loaded = C.load_config(self.path)
        self.assertEqual(loaded["settings"]["timeout_ms"], 1000)
        self.assertTrue(loaded["settings"]["shuffle"])

    def test_save_roundtrip_pretty_and_normalised(self):
        c = C.default_config()
        c["domains"] = ["B.com.", "a.com", "b.com"]
        saved = C.save_config(c, self.path)
        self.assertEqual(saved["domains"], ["b.com", "a.com"])
        text = self.path.read_text()
        self.assertIn('\n  "resolvers": [', text)  # indent=2
        self.assertEqual(C.load_config(self.path), saved)
        leftovers = [p for p in self.path.parent.iterdir() if p.name != "config.json"]
        self.assertEqual(leftovers, [])  # no temp files left behind

    def test_save_invalid_raises_and_leaves_file(self):
        C.save_config(C.default_config(), self.path)
        before = self.path.read_text()
        bad = C.default_config()
        bad["resolvers"] = []
        with self.assertRaises(C.ConfigError) as cm:
            C.save_config(bad, self.path)
        self.assertTrue(cm.exception.errors)
        self.assertEqual(self.path.read_text(), before)

    def test_reset(self):
        c = C.default_config()
        c["domains"] = ["a.com"]
        C.save_config(c, self.path)
        self.assertEqual(C.reset_config(self.path), C.default_config())
        self.assertEqual(C.load_config(self.path), C.default_config())

    def test_estimate(self):
        e = C.estimate(C.default_config())
        self.assertEqual(e["servers"], 8)
        self.assertEqual(e["queries"], 480)
        self.assertEqual(e["max_qps_per_server"], 4.0)
        self.assertEqual(e["max_qps_total"], 32.0)
        self.assertTrue(14 <= e["est_seconds"] <= 20, e)


if __name__ == "__main__":
    unittest.main()
