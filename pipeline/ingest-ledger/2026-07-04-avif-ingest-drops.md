# Ingest ledger — 2026-07-04 avif delta + deliberate drops

Stamp: 2026-07-04 19:38 UTC / 14:38 CDT
Token: FLEET-WORKER1-BUILD-20260704-ingest-96
Seat: Worker1 (delta / Mini)

## What was ingested
Incremental `ingest.py --root $STORAGE_ROOT/photos/originals` (allow-list extended with
`avif`, commit `eb36860`). **96 new assets**, 0 errors:

| ext  | added | notes |
|------|-------|-------|
| WEBP | 58    | full pHash/blur (decodable raster) |
| DNG  | 27    | sha+EXIF only (RAW, no PIL decoder) |
| CR2  | 5     | sha+EXIF only (RAW) |
| CR3  | 3     | sha+EXIF only (RAW) |
| AVIF | 3     | **decodable** — pHash/blur populated, sha set, 0 errors |

assets 102,518 → **102,614** (exact). All 96 rows carry `file_sha256`.

## The 2 deliberate DROPS — NOT cataloged
These two files live under `photos/originals/` but were intentionally left out of the
library. They are on disk and will remain uncatalogued by design; `ingest.py`'s
allow-lists were **not** extended to cover them.

1. `$STORAGE_ROOT/photos/originals/2025/12/209DBDA7-5A8E-4C87-B1E7-18A28FF38049.heics`
   — a `.heics` (HEIC *sequence* / burst container), a single stray double-extension
   file. Not a still we catalog; excluded.
2. `$STORAGE_ROOT/photos/originals/2023/08/IMG_0807.largeThumbnail`
   — a `.largeThumbnail` sidecar (a derived Apple thumbnail, not an original). Excluded
   as a non-original derivative.

Rationale: "everything under photos/ belongs in the library" — these two are not
library-grade originals (one is a burst-sequence container, one is a derived thumbnail),
so the gap is closed **exactly** to the 2 recorded drops rather than by widening the
allow-list to pull in junk extensions.

## Verification (post-ingest)
- CSV re-diff vs `to-worker1/files/photos-inventory-20260704.csv` → **0** uncatalogued
  (all 93 standard-media delta now in the catalog).
- Full-disk diff over `originals/` + `long-video-elsewhere/` → remaining uncatalogued
  media = **exactly these 2 drops** (the only third hit is `originals/.mounted`, the
  MOUNT_SENTINEL infra marker — never a library file).
- 0 db rows point at a missing-on-disk path.
- loupe.service active, HTTP 302 on `/`; faces.db (2026-06-17) / nsfw.db (2026-06-21)
  mtimes untouched.

## Reversibility
- ingest.py backup: `backups/ingest.py.bak-20260704-193353`
- metadata.db pre-ingest snapshot: `backups/metadata.db.pre-avif-ingest-20260704-193421` (102,518 rows)
- run log: `backups/avif-ingest-20260704-193421.log`

## Note (open)
New assets are inert in the running app until `loupe.service` is restarted (import-time
globals: renders/candidates rebuild only at startup). No restart was requested in this
build's scope — flagged for the strategy seat.
