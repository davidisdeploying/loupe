#!/usr/bin/env python3
"""
stage_runner.py — detached worker that owns ONE pipeline run + its durable marker.

Standalone process: NEVER imported. run_control.py spawns it with
start_new_session=True so it survives a loupe.service restart, and sets the child
environment when spawning (this runner inherits its OWN process env verbatim).

It runs the given command (everything after a literal `--`) with cwd=<cwd>, appends
the child's stdout+stderr to <log>, and writes <marker> atomically on every state
change. The runner's own stdout/stderr go nowhere — the marker + log are the record.

Marker schema (JSON):
  {stage, state, pid, attempt, started_at, finished_at, error, raw_tail}
  state ∈ running | retrying | done | failed

Retry policy: exit 0 → done. Nonzero → classify the log tail; a FATAL match stops
immediately (state=failed, no retry); otherwise TRANSIENT → state=retrying, sleep
min(300, 5*2**attempt)s, re-run; after --max-retries attempts → failed "exhausted".

Usage:
  stage_runner.py --stage develop --marker <p> --log <p> --cwd <dir> \
                  [--max-retries 5] -- /usr/bin/python3 ingest.py --root ... --db ...
"""
import argparse
import os
import re
import subprocess
import sys
import time

# Patterns that mean "do not bother retrying" — a misconfiguration or auth failure,
# not a transient NAS/network hiccup. Matched case-insensitively against the log tail.
_FATAL = [re.compile(p, re.I) for p in (
    r"mount sentinel",
    r"not mounted",
    r"no such file or directory",
    r"permission denied",
    r"invalid (?:email|password|credential)",
    r"authentication",
)]

_TAIL_BYTES = 4096


def _tail(path, n=_TAIL_BYTES):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            sz = f.tell()
            f.seek(max(0, sz - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _fatal_match(text):
    """Return the offending text fragment if the tail looks fatal, else None."""
    for rx in _FATAL:
        m = rx.search(text)
        if m:
            return m.group(0)
    return None


def _fatal_reason(text, frag):
    """A short, human-readable line for the marker — the last log line that carries
    the fatal fragment, truncated; falls back to the fragment itself."""
    low = frag.lower()
    for line in reversed(text.splitlines()):
        if low in line.lower():
            return line.strip()[:300]
    return frag


def _write_marker(marker, data):
    """Atomic marker write: tmp + os.replace (same dir, same filesystem)."""
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    tmp = marker + ".tmp"
    with open(tmp, "w") as f:
        import json
        json.dump(data, f)
    os.replace(tmp, marker)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--marker", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--max-retries", type=int, default=5)
    # everything after `--` is the command to run
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        _write_marker(args.marker, {
            "stage": args.stage, "state": "failed", "pid": None, "attempt": 0,
            "started_at": int(time.time()), "finished_at": int(time.time()),
            "error": "no command given after --", "raw_tail": None})
        return

    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    started_at = int(time.time())
    marker = {
        "stage": args.stage, "state": "running", "pid": None, "attempt": 0,
        "started_at": started_at, "finished_at": None, "error": None, "raw_tail": None,
    }

    attempt = 0
    while True:
        attempt += 1
        # child stdout+stderr APPENDED to the log; a header line marks each attempt.
        with open(args.log, "ab") as logf:
            logf.write(("\n==== %s attempt %d @ %s ====\n" % (
                args.stage, attempt, time.strftime("%Y-%m-%dT%H:%M:%S"))).encode())
            logf.flush()
            try:
                proc = subprocess.Popen(cmd, cwd=args.cwd, stdout=logf,
                                        stderr=subprocess.STDOUT)
            except OSError as e:
                # couldn't even launch — treat as fatal (bad path / perms)
                _write_marker(args.marker, {
                    **marker, "state": "failed", "attempt": attempt,
                    "finished_at": int(time.time()),
                    "error": "could not launch: %s" % e, "raw_tail": _tail(args.log)})
                return
            marker.update(state="running", pid=proc.pid, attempt=attempt)
            _write_marker(args.marker, marker)
            rc = proc.wait()

        if rc == 0:
            marker.update(state="done", pid=None, attempt=attempt,
                          finished_at=int(time.time()), error=None,
                          raw_tail=None)
            _write_marker(args.marker, marker)
            return

        tail = _tail(args.log)
        frag = _fatal_match(tail)
        if frag:
            marker.update(state="failed", pid=None, attempt=attempt,
                          finished_at=int(time.time()),
                          error=_fatal_reason(tail, frag), raw_tail=tail)
            _write_marker(args.marker, marker)
            return

        if attempt >= args.max_retries:
            marker.update(state="failed", pid=None, attempt=attempt,
                          finished_at=int(time.time()),
                          error="exhausted %d retries (last exit %d)" % (
                              args.max_retries, rc),
                          raw_tail=tail)
            _write_marker(args.marker, marker)
            return

        # transient: record retrying + backoff, then loop
        backoff = min(300, 5 * (2 ** attempt))
        marker.update(state="retrying", pid=None, attempt=attempt,
                      finished_at=None,
                      error="exit %d; retrying in %ds: %s" % (
                          rc, backoff, (tail.strip().splitlines() or [""])[-1][:200]),
                      raw_tail=tail)
        _write_marker(args.marker, marker)
        time.sleep(backoff)


if __name__ == "__main__":
    main()
