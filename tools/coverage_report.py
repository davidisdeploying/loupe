#!/usr/bin/env python3
"""coverage_report.py — one command for every enrichment gap in the library.

    DATA_ROOT=/data/loupe/state tools/coverage_report.py [--max-gap-pct N]

Exit 0 if every signal is within tolerance, 1 otherwise. Read-only throughout.

Why this exists. On 2026-08-09 an audit item ("close the 34% video-face gap") turned out
to be two different things wearing one number: ~13,658 Live Photo videos excluded on
purpose, and 24 videos from 2026/06-07 that were ingested *after* the one-off video-face
pass ran in July and therefore never entered it at all. The stills pipeline has
`run_control` stages and self-heals on the next run; the video-face pipeline is a
one-shot script, so every newly ingested video silently misses it and the gap grows
without anything reporting a problem.

Coverage drift is invisible precisely because nothing errors. This makes it a number you
can look at, and a non-zero exit you can schedule.
"""
import argparse
import os
import sqlite3
import sys

STATE = os.environ.get("DATA_ROOT", "/data/loupe/state")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEO_EXT = ("MP4", "MOV", "M4V", "AVI", "3GP", "MPG", "MKV", "WEBM")
VIN = ",".join(repr(x) for x in VIDEO_EXT)

METADATA = os.path.join(STATE, "metadata.db")
FACES = os.path.join(STATE, "faces.db")
NSFW = os.path.join(STATE, "nsfw.db")
SIGNALS = os.path.expanduser(
    os.environ.get("LOUPE_VIDEO_SIGNALS_DB", "~/loupe-ml/video/video_signals.db"))


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def ids(conn, sql):
    return {r[0] for r in conn.execute(sql)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-gap-pct", type=float, default=1.0,
                    help="fail above this gap percentage (default 1.0)")
    args = ap.parse_args()

    if not os.path.exists(METADATA):
        print("metadata.db not found at %s — is DATA_ROOT set?" % METADATA)
        return 2

    m = ro(METADATA)
    stills = ids(m, "SELECT id FROM assets WHERE UPPER(extension) NOT IN (%s)" % VIN)
    videos = ids(m, "SELECT id FROM assets WHERE UPPER(extension) IN (%s)" % VIN)
    live = ids(m, "SELECT id FROM assets WHERE UPPER(extension) IN (%s) "
                  "AND is_live_photo_video=1" % VIN)
    nonlive = videos - live

    rows = []

    f = ro(FACES)
    fproc = ids(f, "SELECT asset_id FROM processed")
    rows.append(("faces / stills", len(stills), len(stills & fproc)))
    rows.append(("faces / non-live video", len(nonlive), len(nonlive & fproc)))

    n = ro(NSFW)
    nproc = ids(n, "SELECT asset_id FROM processed")
    rows.append(("nsfw / stills", len(stills), len(stills & nproc)))

    # Video signals gate the video-face pass; a video absent here can never be reached
    # by it, which is exactly how the 2026/06-07 assets went missing.
    if os.path.exists(SIGNALS):
        s = ro(SIGNALS)
        try:
            sha = {r[0] for r in s.execute("SELECT file_sha256 FROM videos")}
            vsha = {r[0]: r[1] for r in m.execute(
                "SELECT id, file_sha256 FROM assets WHERE UPPER(extension) IN (%s)" % VIN)}
            covered = {i for i, h in vsha.items() if h in sha}
            rows.append(("video_signals / video", len(videos), len(covered & videos)))
        except sqlite3.Error as e:
            print("  (video_signals unreadable: %s)" % e)
    else:
        print("  (video_signals.db not found at %s)" % SIGNALS)

    print("%-26s %9s %9s %8s %8s" % ("signal", "total", "covered", "gap", "gap %"))
    print("-" * 66)
    worst = 0.0
    for label, total, covered in rows:
        gap = total - covered
        pct = (100.0 * gap / total) if total else 0.0
        worst = max(worst, pct)
        print("%-26s %9d %9d %8d %7.2f%%" % (label, total, covered, gap, pct))

    print()
    print("Live Photo videos excluded from the video-face pass by design: %d" % len(live))
    print("(their still companions are covered; the ~3s clip adds nothing)")
    print()
    if worst > args.max_gap_pct:
        print("COVERAGE: FAIL — worst gap %.2f%% exceeds %.2f%%" % (worst, args.max_gap_pct))
        return 1
    print("COVERAGE: OK — worst gap %.2f%% (limit %.2f%%)" % (worst, args.max_gap_pct))
    return 0


if __name__ == "__main__":
    sys.exit(main())
