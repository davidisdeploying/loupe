#!/usr/bin/env python3
"""Mutation test for the static invariant suite -- tests the tests.

Breaks one invariant at a time in a COPY of server.py (the live file is never
touched) and asserts the corresponding test fails. A suite that cannot fail guards
nothing; this caught a real hole on the day it was written, where
test_token_compare_is_constant_time matched the string "compare_digest" in the
function's own docstring and so passed even with the constant-time compare removed.

    tests/mutate.py          # exits non-zero if any mutant escapes
"""
import io
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PY = os.environ.get("LOUPE_PYTHON", "/data/loupe-venv/bin/python")
if not os.path.exists(PY):
    PY = sys.executable
SRC = io.open(os.path.join(REPO, "server.py"), encoding="utf-8").read()

MUTANTS = {
    "gate_removed_from_do_POST": (
        "    def do_POST(self):\n        # P2.2 (W23) -- ONE chokepoint.",
        "    def do_POST(self):\n        # MUTANT: gate deleted\n        if False:"),
    "route_added_without_lan_check": (
        '        if urlparse(self.path).path == "/api/decide":\n'
        "            if not self._is_lan_peer():",
        '        if urlparse(self.path).path == "/api/rogue":\n'
        '            return self._json({"ok": True})\n'
        '        if urlparse(self.path).path == "/api/decide":\n'
        "            if not self._is_lan_peer():"),
    "token_compared_with_equals": (
        'return hmac.compare_digest(self.headers.get("X-Loupe-Write-Token") or "", tok)',
        'return (self.headers.get("X-Loupe-Write-Token") or "") == tok'),
    "cf_header_check_dropped": (
        'if h.get("CF-Ray") or h.get("CF-Connecting-IP") or h.get("X-Forwarded-For"):',
        'if h.get("CF-Ray"):'),
    "eviction_call_removed": (
        "                _preview_cache_evict(os.path.getsize(out))",
        "                pass  # MUTANT: eviction removed"),
}


def main():
    tmp = tempfile.mkdtemp(prefix="loupe-mutants-")
    escaped, skipped = [], []
    for name, (old, new) in sorted(MUTANTS.items()):
        if SRC.count(old) != 1:
            # The anchor drifted -- that is a finding, not a pass.
            skipped.append(name)
            print("  %-34s SKIP (anchor matched %d times)" % (name, SRC.count(old)))
            continue
        path = os.path.join(tmp, "server_%s.py" % name)
        io.open(path, "w", encoding="utf-8").write(SRC.replace(old, new, 1))
        p = subprocess.run(
            [PY, "-m", "unittest", "discover", "-s", HERE,
             "-p", "test_static_invariants.py"],
            capture_output=True, text=True, cwd=HERE,
            env=dict(os.environ, LOUPE_SERVER_PY=path))
        fired = p.stderr.count("FAIL:") + p.stderr.count("ERROR:")
        if p.returncode == 0:
            escaped.append(name)
            print("  %-34s *** ESCAPED ***" % name)
        else:
            print("  %-34s CAUGHT  (%d test(s) fired)" % (name, fired))
        os.remove(path)
    os.rmdir(tmp)
    print()
    if escaped or skipped:
        print("MUTATION RESULT: %d escaped, %d anchor(s) drifted" % (len(escaped), len(skipped)))
        return 1
    print("MUTATION RESULT: all %d mutants caught" % len(MUTANTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
