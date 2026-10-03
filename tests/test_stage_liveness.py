"""Stage stall detection (W14).

A pipeline stage's marker records state, pid and started_at -- nothing that advances.
So a wedged stage and a working one look identical, which is the gap W14 names. The
stage log is already an append-only progress stream, so its mtime serves as a heartbeat.

These tests use synthetic markers and logs; they never start a real stage.

The refuse-when-misconfigured case is the important one. Run without DATA_ROOT the CLI
resolves the run-status directory under the repo instead of the state root, finds no
markers, and would otherwise report every stage idle and exit 0 -- a monitor that
reassures you because it is looking in an empty directory. That is the same shape as the
ledger unit reporting SUCCESS for three days while producing no snapshot.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PY = os.environ.get("LOUPE_PYTHON", "/data/loupe-venv/bin/python")
if not os.path.exists(PY):
    PY = sys.executable


def write_stage(root, stage, state, pid, log_age_s):
    """Fabricate a marker + log for one stage inside a throwaway DATA_ROOT."""
    sdir = os.path.join(root, "run-status")
    ldir = os.path.join(root, "run-logs")
    os.makedirs(sdir, exist_ok=True)
    os.makedirs(ldir, exist_ok=True)
    with open(os.path.join(sdir, "%s.status.json" % stage), "w") as fh:
        json.dump({"stage": stage, "state": state, "pid": pid, "attempt": 1,
                   "started_at": int(time.time()) - 100, "finished_at": None,
                   "error": None, "raw_tail": None}, fh)
    log = os.path.join(ldir, "%s.log" % stage)
    with open(log, "w") as fh:
        fh.write("synthetic\n")
    old = time.time() - log_age_s
    os.utime(log, (old, old))
    return log


def liveness_in(root, stage, max_idle):
    """Import run_control against a throwaway DATA_ROOT in a subprocess -- the module
    resolves its paths at import time, so this cannot be done in-process twice."""
    code = (
        "import json,sys;"
        "import run_control as rc;"
        "print(json.dumps(rc.liveness(%r, %r)))" % (stage, max_idle))
    p = subprocess.run([PY, "-c", code], capture_output=True, text=True, cwd=REPO,
                       env=dict(os.environ, DATA_ROOT=root))
    if p.returncode != 0:
        raise AssertionError("liveness failed: %s" % p.stderr[-500:])
    return json.loads(p.stdout.strip().splitlines()[-1])


class StallDetection(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="loupe-stage-")

    def test_running_and_advancing_reads_as_running(self):
        write_stage(self.root, "faces", "running", os.getpid(), log_age_s=5)
        r = liveness_in(self.root, "faces", 1800)
        self.assertEqual(r["verdict"], "running")
        self.assertTrue(r["pid_alive"])

    def test_live_pid_with_a_frozen_log_is_stalled(self):
        write_stage(self.root, "faces", "running", os.getpid(), log_age_s=7200)
        r = liveness_in(self.root, "faces", 1800)
        self.assertEqual(r["verdict"], "stalled")
        self.assertIn("has not advanced", r["reason"])

    def test_dead_pid_with_a_running_marker_is_crashed_not_stalled(self):
        """Different failure, different response: waiting never fixes a lying marker."""
        write_stage(self.root, "faces", "running", 999999, log_age_s=5)
        r = liveness_in(self.root, "faces", 1800)
        self.assertEqual(r["verdict"], "crashed")
        self.assertFalse(r["pid_alive"])

    def test_finished_stage_is_not_reported_stalled(self):
        write_stage(self.root, "faces", "done", None, log_age_s=999999)
        r = liveness_in(self.root, "faces", 1800)
        self.assertEqual(r["verdict"], "done")


class RefusesToReportCleanFromNowhere(unittest.TestCase):
    def test_missing_run_status_dir_exits_2_not_0(self):
        empty = tempfile.mkdtemp(prefix="loupe-empty-")
        p = subprocess.run([PY, "run_control.py", "--check-stalled"],
                           capture_output=True, text=True, cwd=REPO,
                           env=dict(os.environ, DATA_ROOT=empty))
        self.assertEqual(p.returncode, 2,
                         "a check that cannot find its markers must not exit 0")
        self.assertNotIn("clean", p.stdout.lower())

    def test_directory_with_no_markers_exits_2(self):
        root = tempfile.mkdtemp(prefix="loupe-nomark-")
        os.makedirs(os.path.join(root, "run-status"), exist_ok=True)
        p = subprocess.run([PY, "run_control.py", "--check-stalled"],
                           capture_output=True, text=True, cwd=REPO,
                           env=dict(os.environ, DATA_ROOT=root))
        self.assertEqual(p.returncode, 2)
        self.assertIn("refusing", p.stdout.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
