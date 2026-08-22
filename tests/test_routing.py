"""Routing invariants (P9 navigation core).

Loupe is a single-page app whose views are reachable by URL: the server returns the same
shell for nine paths, and the client bootstrap reads `location.pathname` to open the right
view. That only works if the two halves agree.

They did not. The bootstrap recognised seven routes while only three views
(`/map`, `/cutting-room`, `/people`) wrote the URL back. Opening Trips, Settings, Vault or
Flagged left the address bar saying `/`, so a refresh silently dropped you back to the
grid and the view could not be linked.

This is deliberately narrow. It checks that the URL and the view agree — not how
navigation should look or feel, History depth is now pushState per audit 8.5, so Back steps back through spaces;
the tests below require a popstate handler to exist alongside it.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
JS_PATH = os.path.join(REPO, "static", "app.js")
SERVER_PATH = os.path.join(REPO, "server.py")


def read(path):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


JS = read(JS_PATH)
SERVER = read(SERVER_PATH)

# `/places` is a server-side redirect to `/trips`; the bootstrap accepts it for old links
# but nothing should ever write it back.
ALIASES = {"/places"}


def bootstrap_routes():
    """The routes the dispatch table can restore.

    These used to be an if/else chain on location.pathname at the bottom of the file.
    They now live in routeTo(path), which both the first paint and popstate call, so a
    route cannot work on load and silently fail on Back."""
    m = re.search(r"function routeTo\(path\)\{(.*?)\n\}", JS, re.S)
    if not m:
        return set()
    return set(re.findall(r"path===['\"]([^'\"]+)['\"]", m.group(1)))


def written_routes():
    return {m for m in re.findall(r"syncUrl\(\s*['\"](/[^'\"]*)['\"]\s*\)", JS)}


def server_shell_routes():
    m = re.search(r'if p in \(([^)]*)\):\s*\n[^\n]*_html', SERVER)
    if not m:
        return set()
    return set(re.findall(r"['\"](/[^'\"]*)['\"]", m.group(1)))


@unittest.skipUnless(JS, "static/app.js not found")
class Routing(unittest.TestCase):
    def test_bootstrap_recognises_routes(self):
        self.assertGreaterEqual(len(bootstrap_routes()), 7,
                                "the URL bootstrap looks truncated or moved")

    def test_every_bootstrapped_route_is_written_back(self):
        """Otherwise the address bar lies and a refresh loses the view."""
        missing = sorted(bootstrap_routes() - written_routes() - ALIASES)
        self.assertEqual(
            missing, [],
            "these views can be opened by URL but never set it, so refreshing drops "
            "back to the grid: %s" % missing)

    def test_no_view_writes_a_route_the_bootstrap_cannot_open(self):
        """The reverse gap: a URL you can be sent to but that restores nothing."""
        orphan = sorted(written_routes() - bootstrap_routes())
        self.assertEqual(orphan, [],
                         "views set URLs the bootstrap does not handle: %s" % orphan)

    @unittest.skipUnless(SERVER, "server.py not found")
    def test_server_serves_the_shell_for_every_client_route(self):
        """A route the client can set but the server 404s is a broken deep link."""
        shell = server_shell_routes()
        if not shell:
            self.skipTest("could not parse the server shell route list")
        unserved = sorted(r for r in bootstrap_routes() | written_routes()
                          if r not in shell and r not in ALIASES)
        self.assertEqual(unserved, [],
                         "client routes the server does not serve the shell for: %s"
                         % unserved)

    def test_pushstate_is_paired_with_a_popstate_handler(self):
        """The one that matters. pushState WITHOUT popstate is strictly worse than
        replaceState: you create history entries and then Back does nothing when the user
        presses it. Audit 8.5 asks for pushState precisely so Back works."""
        self.assertIn("history.pushState", JS)
        self.assertRegex(JS, r"addEventListener\(\s*['\"]popstate['\"]",
                         "pushState without a popstate handler -- Back would be inert")

    def test_first_paint_and_back_share_one_dispatch_table(self):
        """Otherwise a route can work on load and silently not on Back, or vice versa."""
        self.assertIn("function routeTo(", JS)
        self.assertRegex(JS, r"routeTo\(location\.pathname\)",
                         "the bootstrap no longer goes through routeTo")
        self.assertRegex(JS, r"popstate['\"]\s*,\s*\(\)\s*=>\s*routeTo\(",
                         "popstate no longer goes through routeTo")

    def test_sync_is_guarded_against_duplicate_entries(self):
        """On popstate the browser has already moved location.pathname, so the restored
        view re-calls syncUrl with the same path. Without the guard that pushes a
        duplicate entry and Back appears to do nothing every other press."""
        m = re.search(r"function syncUrl\(([^)]*)\)\{(.*?)\n\}", JS, re.S)
        self.assertIsNotNone(m, "syncUrl is gone")
        self.assertIn("location.pathname===", m.group(2),
                      "syncUrl no longer short-circuits when already on the path")

    def test_every_dispatch_route_is_reachable_from_the_table(self):
        """routeTo must handle every route a view can write, or Back lands nowhere."""
        m = re.search(r"function routeTo\(path\)\{(.*?)\n\}", JS, re.S)
        self.assertIsNotNone(m)
        handled = set(re.findall(r"path===['\"](/[^'\"]*)['\"]", m.group(1)))
        missing = sorted(written_routes() - handled)
        self.assertEqual(missing, [],
                         "views write routes routeTo cannot restore on Back: %s" % missing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
