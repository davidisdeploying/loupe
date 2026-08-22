# LOUPE — Onboarding Runbook (portable spine)

Stand up Loupe against **any** library root on **any** host. Everything below is driven by two
environment variables and a handful of optional overrides — no path is hardcoded. This is the
runbook for standing up a second library on a new user's NAS + Mac (not reachable from the dev box).

> **Out of scope here:** the iCloud-download (`icloudpd`) orchestration and the setup dashboard —
> separate, later. This covers ingest → thumbs → enrichment → faces → summaries → start.

---

## 0. The two roots (set these once, everywhere)

| Var | Meaning | Default when unset |
|---|---|---|
| `LIBRARY_ROOT` | read-only source tree of original media (the NAS mount) | `$STORAGE_ROOT/photos` |
| `DATA_ROOT` | all generated DATA — `metadata.db`, thumb cache, exports, `vendor/`, and loupe's own dbs/caches | historical split: pipeline DATA in the sibling `loupe-pipeline/`, app dbs beside `server.py`. Pipeline SOURCE is separate — in the `pipeline/` subtree |

Originals are expected under `$LIBRARY_ROOT/originals/{YYYY}/{MM}/…`.
With both vars **unset**, the app resolves to the existing on-disk layout (backward-compatible).
Set them to relocate the whole install — e.g. a new host:

```bash
export LIBRARY_ROOT=/srv/photos              # his NAS mount (originals under /srv/photos/originals)
export DATA_ROOT=/srv/loupe-data             # one dir holds metadata.db + thumbs + all app dbs
```

Optional tuning (all have host-aware defaults — leave unset to auto-detect):

| Var | Controls | Default (this 4-core/~8 GB box → value) |
|---|---|---|
| `IMG_WORKERS` | concurrent image decodes (OOM guard) | `12` on ≤12 GB RAM; scales with RAM, capped 48 |
| `VID_WORKERS` | concurrent video frame extracts | `cores×4`, min 8, cap 64 → `16` |
| `THUMB_WORKERS` | latency-bound thumb read fan-out | `200` |
| `INGEST_WORKERS` | ingest I/O thread pool | `os.cpu_count()` → `4` |
| `BACKFILL_WORKERS` | duration-backfill pool | `os.cpu_count()` → `4` |
| `OMP_NUM_THREADS` | insightface CPU threads (faces) | `cores-1` → `3` |
| `LOUPE_REQUIRE_MOUNT` | abort the pipeline if the NAS mount sentinel is missing | off (`0`); set `1` to enforce |
| `MOUNT_SENTINEL` | path of the mount sentinel file | `$LIBRARY_ROOT/originals/.mounted` |

---

## Step 0 — detect the Mac's macOS version (decides the label path)

The Apple **scene/OCR/people labels** come from the Photos *search index*. Its filename and
whether `osxphotos` can read it depends on the macOS version — **run this on the Mac first:**

```bash
sw_vers -productVersion
```

| macOS | Search index | Label path Loupe will use |
|---|---|---|
| ≤ 26 (stable) | `psi.sqlite` | **osxphotos label/search API** — preferred, no hand decoding |
| 27+ (beta)    | `leo.sqlite` (psi successor; osxphotos can't read it yet) | **leo direct-decode** fallback (hand-rolled, in `enrichment/common.py`) |

`enrichment/build.py` **auto-detects** which path works and **logs the one it took**
(`[labels] path = osxphotos API …` vs `[labels] path = leo.sqlite direct-decode …`). You don't
choose manually — you just provide whichever input that macOS exposes (API library vs. a copy of
the index file).

---

## Step 1 — ingest metadata

On the host that can see `$LIBRARY_ROOT` (Linux or Mac):

```bash
cd loupe/pipeline   # pipeline SOURCE now lives in the loupe repo's pipeline/ subtree
python3 ingest.py --root "$LIBRARY_ROOT/originals"        # writes $DATA_ROOT/metadata.db
python3 backfill_duration.py                              # fills video durations
```
(Pipeline DATA — `metadata.db`, the thumb cache, exports, and `vendor/exiftool` — stays in the
sibling `loupe-pipeline/` dir; ingest resolves the vendored exiftool from the `--db` dir.)

- Supported extensions: `heic heif jpg jpeg png mov mp4 m4v gif tiff` (RAW is silently skipped).
- year/month come from the `YYYY/MM` path layout; Live Photos pair by same-stem `HEIC`+`MOV`.
- The NAS-mount guard is **off by default**; set `LOUPE_REQUIRE_MOUNT=1` to abort if unmounted.

## Step 2 — pre-generate thumbnails

```bash
cd loupe
python3 pregen.py                 # images first (IMG_WORKERS), then videos (VID_WORKERS)
```

Thumbs land in `$DATA_ROOT/culling/contactsheets/thumbs/`. Workers auto-tune to the host.

## Step 3 — extract Apple enrichment **on the Mac** (optional but high-value)

Loupe runs fully without this (facts-only mode — blur-ranked cull, review, export all work). With
it you also get aesthetic score chips, the people-protect guard, scene/OCR labels, and the
screenshots/documents pile. All three signals are **Apple-derived**, so extraction happens on the
Mac that holds the Photos library:

```bash
pip install osxphotos        # on the Mac

# a) the UUID↔file bridge + named persons (osxphotos default CSV):
osxphotos export /tmp/_x --export-by-date --report photos-bridge.csv --only-photos --dry-run \
  ; # (any osxphotos run that emits the default 40-col CSV with uuid,original_filename,date,persons)

# b) labels + aesthetic scores:
#    macOS ≤26  → nothing to copy: build.py reads them live via --library (osxphotos API).
#    macOS 27+  → copy the search index out of the library bundle for the leo-decode path:
cp "$HOME/Pictures/Photos Library.photoslibrary/database/search/leo.sqlite" leo-copy.sqlite
#    aesthetic scores when not using --library: a uuid,score_overall CSV → scores.csv
```

Carry `photos-bridge.csv` (always), plus **either** `--library <path>` (stable macOS) **or**
`leo-copy.sqlite` + `scores.csv` (beta macOS) to wherever `metadata.db` lives.

## Step 4 — build `apple-enrichment.db` (reproducible)

```bash
cd loupe/enrichment

# stable macOS (osxphotos API for labels + scores):
python3 build.py --bridge photos-bridge.csv \
    --library "$HOME/Pictures/Photos Library.photoslibrary" \
    --out "$DATA_ROOT/apple-enrichment.new.db"

# beta macOS / Linux extract (leo-decode + scores.csv):
python3 build.py --bridge photos-bridge.csv --leo leo-copy.sqlite --scores scores.csv \
    --out "$DATA_ROOT/apple-enrichment.new.db"
```

Discipline baked in: `metadata.db` is opened **read-only**, the leo/psi index is worked on a
`/tmp` copy, and `--out` **must not already exist** (it refuses to overwrite — so the live
`apple-enrichment.db` is never clobbered; you compare, then swap when satisfied):

```bash
mv "$DATA_ROOT/apple-enrichment.new.db" "$DATA_ROOT/apple-enrichment.db"   # promote when happy
```

The builder prints per-table counts and which label path ran. Ambiguous filenames with no usable
timestamp are left **unbridged** rather than mis-stamped.

## Step 5 — faces (optional, on-host, no Apple data needed)

```bash
cd loupe
python3 faces_pipeline.py            # insightface buffalo_l, CPU; writes $DATA_ROOT/faces.db
python3 seed_apple.py                # optional: seed person suggestions from Apple names
```

## Step 6 — summaries & start

```bash
# optional cloud keys (both gated; app runs without them):
echo 'ANTHROPIC_API_KEY=…'        >> secrets.env     # trip prose
echo 'GOOGLE_PLACES_API_KEY=…'    >> secrets.env     # venue names

python3 server.py 8000               # or: systemctl --user start loupe
```

Summaries (`summaries.db`) build on demand at first view. Full-res originals stay LAN/SSH-gated by
the private-IP guard; front the port with your own tunnel/reverse-proxy for remote access.

---

## Quick scratch sanity check (no real data needed)

```bash
DATA_ROOT=/tmp/loupe-scratch LIBRARY_ROOT=/tmp/lib python3 -c \
  "import server" 2>/dev/null || true     # every db/cache path now resolves under /tmp/loupe-scratch
```

Unset both → the install resolves to its historical paths, unchanged.
