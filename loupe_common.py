#!/usr/bin/env python3
"""loupe_common.py — shared roots + read-only DB helper, centralized to kill the
cross-module drift the recon found (7 near-identical _ro copies, 5× EXCLUDE_SQL,
set-vs-tuple VIDEO_EXT, META-vs-METADATA_DB path blocks).

stdlib ONLY — imports nothing from the project, so it can't create an import cycle.
Imported by the top-level modules at ~/loupe (server, places, summaries, faces_api,
setup_status, faces_pipeline, pregen, seed_apple). The enrichment/ modules live a dir
deeper and keep their own derivation; they do NOT import this.

Paths anchor to THIS file's location (~/loupe), so the values are identical no matter
which module imports it. CODE and DATA are split: pipeline SOURCE lives in pipeline/
(a subtree under ~/loupe — see PIPELINE_DIR), pipeline DATA (metadata.db, thumb cache,
exports, vendor/) stays in the sibling loupe-pipeline/ (V2). With DATA_ROOT unset these
reproduce the historical layout; loupe's own stores (APP_DATA) sit beside this file.
"""
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))                       # ~/loupe
DATA_ROOT = os.environ.get("DATA_ROOT")
V2 = DATA_ROOT or os.path.join(os.path.dirname(HERE), "loupe-pipeline")  # metadata.db lives here
APP_DATA = DATA_ROOT or HERE
METADATA_DB = os.path.join(V2, "metadata.db")
PIPELINE_DIR = os.environ.get("LOUPE_PIPELINE_DIR", os.path.join(HERE, "pipeline"))

# Path-based scope filter appended everywhere assets are read (server/places/summaries/
# faces_pipeline/nsfw_pipeline/pregen). 2026-07-04 (FLEET-WORKER1-BUILD-20260704-app-scope-all):
# David's directive "everything included in the loupe app library" — neutralized to a
# tautology so all ~20 sites keep composing identically; reinstate the exclusion by
# restoring the two clauses on the line below. Former value:
#   EXCLUDE_SQL = ("filepath NOT LIKE '%production/%' "
#                  "AND filepath NOT LIKE '%long-video-elsewhere/%'")
EXCLUDE_SQL = "1=1"

# Video extensions — membership tests only. frozenset is the unified container (the prior
# copies drifted between set and tuple; both were used purely for `ext in VIDEO_EXT`).
VIDEO_EXT = frozenset({"MP4", "MOV", "M4V", "AVI", "3GP", "MPG", "MKV", "WEBM"})


def ro(path):
    """Read-only sqlite connection. Unifies the seven prior _ro/ro copies; every one
    already set row_factory=sqlite3.Row and none set isolation_level/pragmas, so the only
    reconciliation is check_same_thread=False — a safe superset (it merely relaxes a
    same-thread assertion for the single-threaded script callers; query results unchanged)."""
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c
