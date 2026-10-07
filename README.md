# Loupe

**look · cull · loop**

Loupe is a self-hosted review desk for a large personal photo and video library.
It was built to solve one problem: a 91,516-item, ~2.2 TB iCloud export sitting on
a NAS, with no practical way to decide what was worth keeping.

Commercial photo managers optimise for storage and search. Loupe optimises for
*judgment* — presenting the library in the order that makes keep/cut decisions
fastest, recording every decision permanently, and never destroying anything on
its own.

## What it does

- **Ingest** — walks the library tree and builds a SQLite metadata spine
  (EXIF, capture time, dimensions, duration, perceptual hashes, file identity).
- **Culling queues** — rule-based and ML-assisted queries surface near-duplicates,
  burst sequences, blurry frames, and low-signal clips for rapid review.
- **Face clustering** — on-device InsightFace embeddings group people across the
  library so a person can be reviewed, named, and protected as a unit.
- **Aesthetic scoring** — a zero-shot scorer and a preference-learning tool
  (`aesthetic/preftool/`) rank frames within a group so the best of a burst rises.
- **Places** — reverse-geocodes captures onto a bundled basemap for
  location-based review.
- **Safety gating** — an on-device screen sets aside frames that may contain
  nudity. They are owner-only, never shown to shared viewers, and never deleted.
- **Video pipeline** — frame extraction, face passes, and transcode-aware
  handling for the 34k-clip half of the library.

## Design commitments

**Nothing is deleted implicitly.** Decisions are recorded as data. A "cut" marks
an item for a later, explicit, reversible sweep — the review loop never destroys
originals.

**Protected sets are honoured before any rule.** Named people and designated
source classes are excluded from delete candidacy at the query layer, not by
convention.

**Owner data never enters the repository.** Real names, residences, credentials,
and the library itself live outside version control. `RELEASE-BOUNDARY.md`
documents the split, and `tests/test_release_boundary.py` fails the build if a
secret or a personal settings file ever becomes tracked. This is enforced, not
asserted.

**The install is portable.** Two environment variables — `LIBRARY_ROOT` and
`DATA_ROOT` — relocate an entire install to another host. No path is hardcoded.
Worker pool sizes auto-detect from available cores and RAM.

## Stack

Python 3 standard-library HTTP server, SQLite (WAL), pillow-heif, InsightFace,
and a dependency-free vanilla-JS front end. No framework, no build step, no
runtime cloud dependency. It was developed to run acceptably on a 4-thread,
8 GB the compute host against a CIFS-mounted NAS, which shaped most of the concurrency
and caching decisions.

## Running it

`ONBOARDING.md` is the full stand-up runbook for pointing Loupe at a new library
on a new host. `OPERATIONS.md` covers the deployed service, state layout, and
backup posture. `RELEASE-BOUNDARY.md` describes exactly what ships, what stays
private, and what a fresh install regenerates for itself.

```bash
export LIBRARY_ROOT=/srv/photos        # originals under $LIBRARY_ROOT/originals/YYYY/MM/
export DATA_ROOT=/srv/loupe-data       # all generated data and databases
python3 server.py
```

## Status

Loupe is a personal project, run daily against a live library. It is published
as a portfolio piece rather than as a supported product — expect the setup to
assume a Linux host, a mounted library root, and some willingness to read the
runbook.

## License

MIT — see [LICENSE](LICENSE).
