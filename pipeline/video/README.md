# pipeline/video/ — Stage 2a video-face pass (vendored from `~/loupe-ml/video`, 2026-08-09)

Versioned copies of the **load-bearing** scripts from the unversioned `~/loupe-ml/video`
working directory (W19). That directory is 471 MB of July 2026 exploration — dry runs,
seven `run_full.py.bak-*` generations, logs, sampled frames — with three genuinely
production scripts buried in it and no version control at all.

**The copies here are canonical from now on.** The originals stay in place because they
sit beside the live data they wrote (`video_signals.db`, `video-faces/progress.db`, and
the 108-shard export bundle); moving those would break paths baked into the scripts. Edit
here, not there.

| file | role | writes |
|---|---|---|
| `video_face_pass.py` | Stage 2a — portable buffalo_l face embeddings for non-Live-Photo videos | `video-faces/progress.db`, `video-faces/export/faces_shard_*.npz` + `assets_processed.jsonl` |
| `videoscan.py` | video signal extraction | `video_signals.db` |
| `run_full.py` | full-run orchestrator over the video library | `video_signals.db` |

Everything else in `~/loupe-ml/video` is one-off: `cull_dryrun.py`, `cull_dryrun_v2_sha.py`,
`report.py`, `report_v2.py`, `run_sample.py`, `backfill_identity.py`, `job_status_server.py`,
plus `.bak` generations and logs. Not vendored; archive when convenient.

## Path configuration (de-hardcoded 2026-08-15)

The three load-bearing constants now follow the sanctioned env-with-default form,
so these scripts can be relocated without editing them:

| env var | default | used by |
|---|---|---|
| `LOUPE_VIDEO_BASE` | `~/loupe-ml/video` | `videoscan.py`, `video_face_pass.py` (and `run_full.py` via `vs.BASE`) |
| `LOUPE_INSIGHTFACE_ROOT` | `/data/loupe-insightface` | `video_face_pass.py` |
| `LOUPE_CIFS_MOUNT` | `$STORAGE_ROOT` | `run_full.py` |
| `LOUPE_VIDEO_META_DB` | `/data/loupe/state/metadata.db` | `video_face_pass.py` |

Defaults are unchanged from the hard-coded values, so behaviour is identical
unless a variable is set. `tests/test_portability.py` no longer allowlists this
directory: its mount check now uses an ast docstring walk, so prose may cite
real paths while executable lines are held to the env-with-default form.

The copies here and in `~/loupe-ml/video` remain sha256-identical; every edit must
be applied to BOTH. That identity is the provenance claim, and as of 2026-08-15 it
is enforced by `tests/test_video_vendoring.py` rather than by discipline alone — a
one-sided edit now fails the suite instead of forking the pair silently.

## META_DB (2026-08-15)

`video_face_pass.py` built its worklist from `video-faces/metadata.sky.bak`, a
2026-07-05 snapshot of *delta*'s metadata.db taken before Loupe moved hosts. It
now reads the live `metadata.db` instead.

Safe because the query touches only `(id, file_sha256)` — the file paths come from
`video_signals.db`, joined by sha, so the worklist is content-addressed. Snapshot
and live agreed on every one of the 102,614 shared ids with zero sha disagreements,
and both produce an **identical** 26,983-asset worklist, so the switch was a
verified no-op on the day it was made.

The snapshot had drifted 64 assets (24 videos) behind live. Those 24 do not join
the worklist yet — they are absent from `video_signals.db` and so count as
sha-unmatched; they become eligible once `videoscan.py` covers them. That is the
point of tracking live rather than a frozen copy.

Trade-off: a snapshot is stable, so two runs on different days built the same
worklist. Against live they can differ as the library grows. Acceptable here
because the pass is idempotent (`INSERT OR IGNORE`); set `LOUPE_VIDEO_META_DB` at a
pinned copy if a reproducible worklist is ever needed.
