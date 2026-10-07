"""Structural invariants of server.py, proven by parsing it (see tests/README.md)."""
import ast
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# LOUPE_SERVER_PY lets the harness run against a mutated copy, which is how these
# tests are themselves tested -- a test that cannot fail guards nothing.
SERVER = os.environ.get("LOUPE_SERVER_PY") or os.path.join(os.path.dirname(HERE), "server.py")
SRC = open(SERVER, encoding="utf-8").read()
TREE = ast.parse(SRC)


def _class(name):
    for n in ast.walk(TREE):
        if isinstance(n, ast.ClassDef) and n.name == name:
            return n
    raise AssertionError("class %s not found in server.py" % name)


def _method(cls, name):
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError("method %s not found on %s" % (name, cls.name))


def _func(name):
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError("function %s not found at module level" % name)


class WriteGate(unittest.TestCase):
    """P2.2 / W23. The whole design rests on one chokepoint; assert it is still one."""

    def setUp(self):
        self.h = _class("H")
        self.do_post = _method(self.h, "do_POST")

    def test_gate_is_the_first_statement_of_do_POST(self):
        first = self.do_post.body[0]
        self.assertIsInstance(
            first, ast.If,
            "do_POST must open with the write-token guard; a route was added above it")
        calls = [n.func.attr for n in ast.walk(first.test)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        self.assertIn("_write_authorized", calls,
                      "the first statement of do_POST is no longer the write gate")

    def test_gate_runs_before_any_body_read(self):
        """An unauthorized peer must never get to send us a payload."""
        gate_line = self.do_post.body[0].lineno
        for n in ast.walk(self.do_post):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "read"):
                self.assertGreater(n.lineno, gate_line,
                                   "rfile.read() at line %d precedes the write gate" % n.lineno)

    def test_every_post_route_also_checks_is_lan_peer(self):
        """The token is layered ON TOP of the LAN check, never instead of it -- that is
        what makes a stolen token useless over the Cloudflare tunnel."""
        routes, guarded = [], 0
        for n in self.do_post.body:
            if not isinstance(n, ast.If):
                continue
            consts = [c.value for c in ast.walk(n.test)
                      if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            paths = [c for c in consts if c.startswith("/api/")]
            if not paths:
                continue
            routes.append(paths[0])
            names = [x.func.attr for x in ast.walk(n) if isinstance(x, ast.Call)
                     and isinstance(x.func, ast.Attribute)]
            if "_is_lan_peer" in names:
                guarded += 1
        self.assertGreaterEqual(len(routes), 16, "expected >=16 POST routes, found %d" % len(routes))
        self.assertEqual(guarded, len(routes),
                         "these POST routes lack an _is_lan_peer check: %s"
                         % [r for r in routes])

    def test_token_compare_is_constant_time(self):
        """Assert on the call node, not the source text.

        An earlier version of this test matched the string "compare_digest" anywhere in
        the function -- which the docstring also contains, so swapping the real compare
        for == still passed. Mutation testing caught it; hence the AST walk."""
        fn = _method(self.h, "_write_authorized")
        calls = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                calls.append(n.func.attr)
        self.assertIn("compare_digest", calls,
                      "_write_authorized no longer calls hmac.compare_digest")
        for n in ast.walk(fn):
            if isinstance(n, ast.Compare):
                for op in n.ops:
                    self.assertNotIsInstance(
                        op, ast.Eq,
                        "token compared with == at line %d — timing oracle" % n.lineno)

    def test_unconfigured_token_leaves_the_gate_inert(self):
        """Arm/disarm has to stay a one-key settings edit; if this stops being true the
        documented disarm procedure silently breaks."""
        seg = ast.get_source_segment(SRC, _method(self.h, "_write_authorized")) or ""
        self.assertRegex(seg, r"if not tok:\s*\n\s*return True")


class LanPeerGate(unittest.TestCase):
    def test_rejects_all_three_cloudflare_headers(self):
        seg = ast.get_source_segment(SRC, _method(_class("H"), "_is_lan_peer")) or ""
        for header in ("CF-Ray", "CF-Connecting-IP", "X-Forwarded-For"):
            self.assertIn(header, seg, "_is_lan_peer no longer rejects %s" % header)

    def test_uses_socket_peer_not_the_host_header(self):
        seg = ast.get_source_segment(SRC, _method(_class("H"), "_is_lan_peer")) or ""
        self.assertIn("client_address", seg)
        self.assertNotIn('headers.get("Host"', seg)


class PreviewCache(unittest.TestCase):
    """W24. Eviction is only sound while build_preview is the sole writer."""

    def test_build_preview_triggers_eviction(self):
        seg = ast.get_source_segment(SRC, _func("build_preview")) or ""
        self.assertIn("_preview_cache_evict", seg,
                      "build_preview writes to the cache without an eviction check")

    def test_eviction_never_fails_a_request(self):
        seg = ast.get_source_segment(SRC, _func("build_preview")) or ""
        idx = seg.find("_preview_cache_evict")
        self.assertGreater(idx, -1)
        self.assertIn("try:", seg[max(0, idx - 200):idx],
                      "the eviction call must be wrapped so it cannot fail a request")

    def test_eviction_skips_partial_writes(self):
        seg = ast.get_source_segment(SRC, _func("_preview_cache_evict")) or ""
        self.assertIn(".tmp", seg, "eviction must never consider a partial .tmp write")

    def test_cap_is_configurable_and_disableable(self):
        self.assertIn("LOUPE_PREVIEW_CACHE_MAX_MB", SRC)
        seg = ast.get_source_segment(SRC, _func("_preview_cache_evict")) or ""
        self.assertIn("<= 0", seg, "cap of 0 must disable eviction")


if __name__ == "__main__":
    unittest.main(verbosity=2)
