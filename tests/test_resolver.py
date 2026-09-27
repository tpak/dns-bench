from __future__ import annotations

import contextlib
import os
import socket
import struct
import threading
import time
import unittest

from dnsbench import resolver as R


def make_response(query: bytes, rcode=0, ancount=1, qid=None, qr=True, tc=False) -> bytes:
    """Build a reply to ``query``: same question, header flags as requested."""
    orig_id, flags = struct.unpack_from("!HH", query, 0)
    flags = (flags & 0x0100) | 0x0080 | (0x8000 if qr else 0) | (0x0200 if tc else 0) | rcode
    header = struct.pack("!HHHHHH", orig_id if qid is None else qid, flags, 1, ancount, 0, 0)
    body = query[12:]
    answer = b""
    for _ in range(ancount):  # A record 1.2.3.4 with a compression pointer to the qname
        answer += struct.pack("!HHHIH", 0xC00C, 1, 1, 60, 4) + bytes([1, 2, 3, 4])
    return header + body + answer


class MockDNS:
    """UDP server on loopback. ``behaviour(query, n)`` returns a list of
    (delay_s, bytes) replies to send for the n-th query (0-based)."""

    def __init__(self, behaviour, family=socket.AF_INET, host="127.0.0.1"):
        self.behaviour = behaviour
        self.sock = socket.socket(family, socket.SOCK_DGRAM)
        self.sock.bind((host, 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.received = []
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            n = len(self.received)
            self.received.append((data, addr))
            for delay, reply in self.behaviour(data, n):
                if delay:
                    time.sleep(delay)
                with contextlib.suppress(OSError):
                    self.sock.sendto(reply, addr)

    def close(self):
        self._stop.set()
        self.thread.join(1)
        self.sock.close()


class PacketTest(unittest.TestCase):
    def test_build_query_exact_bytes(self):
        pkt = R.build_query(0x1234, "example.com", "A")
        expected = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
        self.assertEqual(pkt, expected)

    def test_build_query_aaaa_and_trailing_dot(self):
        pkt = R.build_query(1, "a.b.", "AAAA")
        self.assertEqual(pkt[12:], b"\x01a\x01b\x00\x00\x1c\x00\x01")
        self.assertEqual(R.build_query(1, "a.b", 28), pkt)

    def test_build_query_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            R.build_query(1, "a..b")
        with self.assertRaises(ValueError):
            R.build_query(1, "x" * 64 + ".com")
        with self.assertRaises(ValueError):
            R.build_query(1, "")
        with self.assertRaises(ValueError):
            R.build_query(1, "a.com", "MX")
        with self.assertRaises(ValueError):
            R.build_query(70000, "a.com")

    def test_build_query_idna(self):
        pkt = R.build_query(1, "bücher.de")
        self.assertIn(b"xn--bcher-kva", pkt)

    def test_parse_response(self):
        q = R.build_query(0xBEEF, "example.com")
        h = R.parse_response(make_response(q, rcode=3, ancount=0, tc=True))
        self.assertEqual(h["id"], 0xBEEF)
        self.assertTrue(h["qr"])
        self.assertTrue(h["tc"])
        self.assertTrue(h["rd"])
        self.assertEqual(h["rcode"], 3)
        self.assertEqual(h["rcode_name"], "NXDOMAIN")
        self.assertEqual(h["ancount"], 0)
        self.assertEqual(h["qdcount"], 1)
        h = R.parse_response(make_response(q, rcode=0, ancount=2))
        self.assertEqual((h["rcode_name"], h["ancount"]), ("NOERROR", 2))

    def test_parse_short(self):
        with self.assertRaises(ValueError):
            R.parse_response(b"\x00" * 11)

    def test_query_ids_random(self):
        ids = {R.new_query_id() for _ in range(200)}
        self.assertGreater(len(ids), 150)
        self.assertTrue(all(0 <= i <= 0xFFFF for i in ids))

    def test_addr_matches(self):
        import ipaddress

        self.assertTrue(R.addr_matches("1.1.1.1", ipaddress.ip_address("1.1.1.1")))
        self.assertFalse(R.addr_matches("1.1.1.2", ipaddress.ip_address("1.1.1.1")))
        self.assertTrue(R.addr_matches("::ffff:1.1.1.1", ipaddress.ip_address("1.1.1.1")))
        self.assertTrue(R.addr_matches("fe80::1%en0", "fe80::1"))
        self.assertFalse(R.addr_matches("garbage", ipaddress.ip_address("1.1.1.1")))


class QueryTest(unittest.TestCase):
    def serve(self, behaviour, **kw):
        m = MockDNS(behaviour, **kw)
        self.addCleanup(m.close)
        return m

    def test_ok(self):
        m = self.serve(lambda q, n: [(0.01, make_response(q, ancount=2))])
        r = R.query("127.0.0.1", "example.com", port=m.port, timeout_s=1)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.rcode, "NOERROR")
        self.assertEqual(r.answers, 2)
        self.assertEqual(r.attempts, 1)
        self.assertIsNone(r.error)
        self.assertGreaterEqual(r.ms, 9.0)  # includes the 10 ms server delay
        self.assertLess(r.ms, 500)
        # the question on the wire is what we built
        data, _ = m.received[0]
        self.assertEqual(data[12:], R.build_query(0, "example.com")[12:])
        self.assertEqual(struct.unpack_from("!H", data, 2)[0], 0x0100)

    def test_nxdomain_is_ok(self):
        m = self.serve(lambda q, n: [(0, make_response(q, rcode=3, ancount=0))])
        r = R.query("127.0.0.1", "nope.invalid", port=m.port)
        self.assertEqual((r.status, r.rcode, r.answers), ("ok", "NXDOMAIN", 0))

    def test_servfail_and_refused_are_errors_with_latency(self):
        for rcode, name in ((2, "SERVFAIL"), (5, "REFUSED")):
            with self.subTest(rcode=name):
                m = self.serve(lambda q, n, rc=rcode: [(0, make_response(q, rcode=rc, ancount=0))])
                r = R.query("127.0.0.1", "example.com", port=m.port)
                self.assertEqual(r.status, "error")
                self.assertEqual(r.rcode, name)
                self.assertIsNotNone(r.ms)

    def test_truncated_is_ok(self):
        m = self.serve(lambda q, n: [(0, make_response(q, tc=True))])
        r = R.query("127.0.0.1", "example.com", port=m.port)
        self.assertEqual(r.status, "ok")
        self.assertTrue(r.truncated)

    def test_timeout(self):
        m = self.serve(lambda q, n: [])
        t0 = time.perf_counter()
        r = R.query("127.0.0.1", "example.com", port=m.port, timeout_s=0.2)
        took = time.perf_counter() - t0
        self.assertEqual(r.status, "timeout")
        self.assertIsNone(r.ms)
        self.assertEqual(r.attempts, 1)
        self.assertGreaterEqual(took, 0.19)
        self.assertLess(took, 1.0)

    def test_wrong_id_and_non_response_ignored(self):
        def behaviour(q, n):
            real_id = struct.unpack_from("!H", q, 0)[0]
            return [
                (0, make_response(q, qid=(real_id + 1) & 0xFFFF)),  # wrong ID
                (0, make_response(q, qr=False)),  # QR=0
                (0, b"\x00\x01"),  # garbage
                (0.02, make_response(q, rcode=3, ancount=0)),  # the real one
            ]

        m = self.serve(behaviour)
        r = R.query("127.0.0.1", "example.com", port=m.port, timeout_s=1)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.rcode, "NXDOMAIN")  # proves the decoys were skipped
        self.assertGreaterEqual(r.ms, 19.0)

    def test_retry_only_after_timeout(self):
        m = self.serve(lambda q, n: [] if n == 0 else [(0, make_response(q))])
        r = R.query("127.0.0.1", "example.com", port=m.port, timeout_s=0.2, tries=2)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.attempts, 2)
        self.assertLess(r.ms, 150)  # latency of the successful attempt only
        self.assertEqual(len(m.received), 2)
        # fresh socket per attempt => fresh source port, fresh ID
        (d1, a1), (d2, a2) = m.received
        self.assertNotEqual((a1[1], d1[:2]), (a2[1], d2[:2]))

    def test_no_retry_after_answer(self):
        m = self.serve(lambda q, n: [(0, make_response(q, rcode=2, ancount=0))])
        r = R.query("127.0.0.1", "example.com", port=m.port, tries=3)
        self.assertEqual((r.status, r.attempts), ("error", 1))
        self.assertEqual(len(m.received), 1)

    def test_tries_exhausted(self):
        m = self.serve(lambda q, n: [])
        r = R.query("127.0.0.1", "example.com", port=m.port, timeout_s=0.1, tries=3)
        self.assertEqual((r.status, r.attempts), ("timeout", 3))
        self.assertEqual(len(m.received), 3)

    def test_ipv6_loopback(self):
        if not socket.has_ipv6:
            self.skipTest("no IPv6")
        try:
            m = self.serve(lambda q, n: [(0, make_response(q))], family=socket.AF_INET6, host="::1")
        except OSError:
            self.skipTest("IPv6 loopback unavailable")
        r = R.query("::1", "example.com", "AAAA", port=m.port)
        self.assertEqual(r.status, "ok")
        data, _ = m.received[0]
        self.assertEqual(data[-4:], b"\x00\x1c\x00\x01")

    def test_invalid_server(self):
        r = R.query("not-an-ip", "example.com")
        self.assertEqual(r.status, "error")
        self.assertIn("invalid server", r.error)

    def test_invalid_domain(self):
        r = R.query("127.0.0.1", "a..b", port=9)
        self.assertEqual(r.status, "error")

    @unittest.skipUnless(os.environ.get("DNSBENCH_LIVE") == "1", "set DNSBENCH_LIVE=1 for live DNS")
    def test_live_cloudflare(self):
        r = R.query("1.1.1.1", "example.com", timeout_s=2)
        self.assertEqual(r.status, "ok", r)
        self.assertGreater(r.answers, 0)


if __name__ == "__main__":
    unittest.main()
