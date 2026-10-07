"""Live-service contract: golden response shapes + the full P2.2 security matrix.

Codifies the verification that was run by hand when W23 shipped, so the write gate
cannot quietly regress. Skips itself when the service is not reachable.
"""
import json
import os
import socket
import subprocess
import unittest
import urllib.error
import urllib.request

PORT = int(os.environ.get("LOUPE_TEST_PORT", "8000"))
LOOPBACK = "http://127.0.0.1:%d" % PORT
SETTINGS = os.environ.get("LOUPE_SETTINGS",
                          "/data/loupe/state/loupe-settings.json")
NOOP_WRITE = json.dumps({"items": [], "state": "undecided"}).encode()


def lan_addr():
    """This host's own LAN address — a non-loopback peer, which is the whole point.

    Overridable because the interface a test runner sits on is not knowable here."""
    if os.environ.get("LOUPE_TEST_LAN_ADDR"):
        return os.environ["LOUPE_TEST_LAN_ADDR"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.3", 1))       # no packet is sent; just picks the route
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def write_token():
    try:
        with open(SETTINGS) as f:
            t = json.load(f).get("write_token")
        return t.strip() if isinstance(t, str) else ""
    except Exception:
        return ""


def req(url, method="GET", headers=None, body=None, timeout=30):
    r = urllib.request.Request(url, method=method, data=body)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def service_up():
    try:
        return req(LOOPBACK + "/", timeout=5)[0] == 200
    except Exception:
        return False


@unittest.skipUnless(service_up(), "loupe.service not reachable on %s" % LOOPBACK)
class ReadContract(unittest.TestCase):
    def test_root_serves_html(self):
        code, body = req(LOOPBACK + "/")
        self.assertEqual(code, 200)
        self.assertIn(b"<!doctype html", body[:200].lower())

    def test_overview_golden_shape(self):
        code, body = req(LOOPBACK + "/api/overview")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIn("years", data)
        self.assertTrue(data["years"], "overview returned no years")
        for key in ("year", "total", "decided", "pct"):
            self.assertIn(key, data["years"][0],
                          "/api/overview year rows lost the %r field" % key)
        self.assertIsInstance(data["years"][0]["total"], int)

    def test_on_this_day_golden_shape(self):
        code, body = req(LOOPBACK + "/api/on-this-day?m=1&d=1")
        self.assertEqual(code, 200)
        data = json.loads(body)
        for key in ("label", "m", "d", "items", "summary"):
            self.assertIn(key, data, "/api/on-this-day lost the %r field" % key)
        for key in ("total", "decided", "cut", "pct"):
            self.assertIn(key, data["summary"])

    def test_on_this_day_rejects_bad_date(self):
        code, body = req(LOOPBACK + "/api/on-this-day?m=13&d=1")
        self.assertEqual(code, 200)
        self.assertIn("error", json.loads(body))

    def test_calendar_golden_shape(self):
        code, body = req(LOOPBACK + "/api/calendar")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIn("days", data)
        self.assertEqual(len(data["days"]), 366,
                          "calendar should cover every day Jan 1 - Dec 31, incl. Feb 29")
        for key in ("m", "d", "label", "total", "decided", "cut", "pct", "state"):
            self.assertIn(key, data["days"][0], "/api/calendar day rows lost the %r field" % key)
        self.assertIn("summary", data)

    def test_setup_page_serves(self):
        self.assertEqual(req(LOOPBACK + "/setup")[0], 200)

    def test_client_write_wrapper_is_served(self):
        """The header is useless if the browser never attaches it."""
        for path in ("/static/app.js", "/setup"):
            code, body = req(LOOPBACK + path)
            self.assertEqual(code, 200)
            self.assertIn(b"loupe_write_token", body,
                          "%s no longer ships the write-token fetch wrapper" % path)


@unittest.skipUnless(service_up(), "loupe.service not reachable on %s" % LOOPBACK)
class WriteGateMatrix(unittest.TestCase):
    """/api/decide with an empty item list is a no-op write — it exercises the gate
    without recording a decision."""

    URL_PATH = "/api/decide"
    JSON = {"Content-Type": "application/json"}

    def setUp(self):
        self.tok = write_token()
        self.lan = lan_addr()

    def lan_url(self):
        if not self.lan:
            self.skipTest("no non-loopback address available")
        return "http://%s:%d%s" % (self.lan, PORT, self.URL_PATH)

    def test_loopback_is_exempt(self):
        code, _ = req(LOOPBACK + self.URL_PATH, "POST", self.JSON, NOOP_WRITE)
        self.assertEqual(code, 200, "server-local tooling must keep working")

    def test_guest_tunnel_write_is_refused(self):
        h = dict(self.JSON, **{"CF-Ray": "harness-test"})
        code, body = req(LOOPBACK + self.URL_PATH, "POST", h, NOOP_WRITE)
        self.assertEqual(code, 403)
        self.assertEqual(json.loads(body).get("error"), "LAN only")

    def test_guest_tunnel_refused_even_with_a_valid_token(self):
        """The token is layered on top of the LAN check. If this ever returns 200, a
        leaked token has become a remote write capability over the public tunnel."""
        if not self.tok:
            self.skipTest("write gate not armed")
        h = dict(self.JSON, **{"CF-Ray": "harness-test",
                               "X-Loupe-Write-Token": self.tok})
        code, body = req(self.lan_url(), "POST", h, NOOP_WRITE)
        self.assertEqual(code, 403)
        self.assertEqual(json.loads(body).get("error"), "LAN only")

    def test_lan_peer_without_token_is_refused(self):
        if not self.tok:
            self.skipTest("write gate not armed")
        code, body = req(self.lan_url(), "POST", self.JSON, NOOP_WRITE)
        self.assertEqual(code, 403, "a wifi guest can write — W23 has regressed")
        self.assertEqual(json.loads(body).get("error"), "write token required")

    def test_lan_peer_with_wrong_token_is_refused(self):
        if not self.tok:
            self.skipTest("write gate not armed")
        h = dict(self.JSON, **{"X-Loupe-Write-Token": "not-the-token"})
        code, _ = req(self.lan_url(), "POST", h, NOOP_WRITE)
        self.assertEqual(code, 403)

    def test_lan_peer_with_correct_token_succeeds(self):
        if not self.tok:
            self.skipTest("write gate not armed")
        h = dict(self.JSON, **{"X-Loupe-Write-Token": self.tok})
        code, _ = req(self.lan_url(), "POST", h, NOOP_WRITE)
        self.assertEqual(code, 200, "owner devices are locked out")

    def test_darkroom_console_is_owner_only(self):
        """8.5: the Darkroom is _is_lan_peer-gated. It was not -- /setup answered 200 to
        any tunnel guest, exposing library roots, absolute paths and per-stage counts.
        Obscure is not gated, and it stopped being obscure once a Setup button appeared
        in the rail."""
        h = {"CF-Ray": "harness-test"}
        for path in ("/setup", "/api/setup/status"):
            with self.subTest(path=path):
                code, _ = req(LOOPBACK + path, headers=h)
                self.assertEqual(code, 403,
                                 "%s is reachable by a guest" % path)

    def test_darkroom_console_still_serves_the_owner(self):
        for path in ("/setup", "/api/setup/status"):
            with self.subTest(path=path):
                self.assertEqual(req(LOOPBACK + path)[0], 200,
                                 "%s stopped serving the owner" % path)

    def test_reads_stay_open_to_lan_peers(self):
        if not self.lan:
            self.skipTest("no non-loopback address available")
        code, _ = req("http://%s:%d/api/overview" % (self.lan, PORT))
        self.assertEqual(code, 200, "the write gate must not have caught read paths")


if __name__ == "__main__":
    unittest.main(verbosity=2)
