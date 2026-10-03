# Photo Library Metadata Ingest — Build Brief

## Context

I have a 91,516-item photo library exported from iCloud to my NAS at 
`$STORAGE_ROOT/photos/originals/`, organized as `{YYYY}/{MM}/IMG_NNNN.HEIC` etc.
Total ~2.2TB. File types: HEIC (15,675), MOV (30,889), JPG (33,144), 
PNG (7,480), MP4 (3,819).

Hardware: Intel the compute host A1347, Ubuntu 24.04, Intel i5-4278U (4 threads), 
8GB RAM + 4GB swap, 107GB local SSD (90GB free). NAS mounted via CIFS at 
$STORAGE_ROOT. CIFS is slow for many small file ops — keep that in mind.

This box is now dedicated to the photo project. No other workloads.

I will later cull this library using rule-based + ML-assisted queries 
(Gallery will come later). This script is the foundation that everything 
else builds on.

## Goal

Build a Python script that walks the photo tree and populates a SQLite 
database with rich metadata, perceptual hashes, and quality scores so I 
can later run culling queries against it.

The database itself lives on the LOCAL SSD at 
~/loupe-pipeline/metadata.db, NOT on the NAS — small reads/writes are 
vastly faster on local disk and CIFS chokes on SQLite locking patterns.

## Schema (target)

One main `assets` table with these columns at minimum:

- id (primary key, integer autoincrement)
- filepath (unique, full path on NAS)
- filename (basename)
- file_size_bytes
- file_mtime (unix timestamp)
- file_sha256 (content hash for true-duplicate detection)
- mime_type
- extension (HEIC, MOV, JPG, PNG, MP4, etc.)
- year (extracted from path)
- month (extracted from path)
- 
- -- EXIF / metadata
- capture_timestamp (unix timestamp when photo was taken, null if missing)
- gps_lat (nullable)
- gps_lon (nullable)
- camera_make (nullable)
- camera_model (nullable)
- lens_model (nullable)
- iso (nullable)
- shutter_speed (nullable)
- aperture (nullable)
- width_pixels
- height_pixels
- orientation
- 
- -- Live Photo / shared album detection
- is_live_photo_still (bool — HEIC with matching MOV companion)
- is_live_photo_video (bool — MOV companion of a HEIC)
- live_photo_partner_id (FK to other half of pair, nullable)
- is_shared_album (bool — filename starts with 'od_' or contains 
  '_singular_display')
- 
- -- Quality / similarity
- phash (16-char hex perceptual hash, null for video files)
- dhash (16-char hex difference hash, null for video files)
- blur_laplacian (float, higher = sharper, null for video files)
- 
- -- Video-specific
- duration_seconds (nullable, only for videos)
- 
- -- Processing tracking
- processed_at (unix timestamp when this row was filled)
- processing_errors (text, null if clean)

Add indexes on: year, month, capture_timestamp, (gps_lat, gps_lon), 
phash, file_sha256, is_shared_album, is_live_photo_still, 
is_live_photo_video.

## Behavior Requirements

1. **Resumable.** Should be safe to interrupt and restart. On startup, 
   skip files that already exist in the DB with non-null processed_at. 
   91k files at slow CIFS speeds will take hours; SSH disconnects 
   happen.

2. **Robust to errors.** Bad EXIF, corrupted images, weird file types — 
   record what we can, set `processing_errors`, keep going. Do not 
   crash the whole job because one HEIC has a broken EXIF block.

3. **Progress reporting.** Print progress every 100 files: "Processed 
   N/total (X%), errors: M, est. time remaining: H:MM".

4. **Parallel where safe.** Use a ThreadPoolExecutor with 4-8 workers 
   for I/O-bound parts (file reads, hashing).

5. **Live Photo pairing.** Identify pairs by matching basenames in the 
   same directory. IMG_3277.HEIC + IMG_3277_HEVC.MOV is a pair. 
   IMG_3277.HEIC + IMG_3277.MOV is also a pair. Set flags on both 
   rows.

6. **Don't re-hash on resume.** If we have a row with the same 
   filepath, same file_size_bytes, and same file_mtime, trust it's the 
   same file and skip re-hashing.

## Tooling

- `exiftool` already installed — use via subprocess with `-j` for JSON.
- `imagehash` library for pHash and dHash.
- `opencv-python-headless` for Laplacian blur score.
- `Pillow` + `pillow-heif` for reading HEIC.
- `ffprobe` (ffmpeg) for video duration.
- sqlite3 from stdlib, no ORM.

For pHash on HEIC: `from pillow_heif import register_heif_opener; 
register_heif_opener()` at the top.

## Failure Modes to Anticipate

- CIFS mount drops. Check 
  `$STORAGE_ROOT/photos/originals/.mounted` exists before starting; abort 
  with clear error if not. Re-check every 1000 files.
- HEIC without EXIF (rare but happens with screenshots saved as HEIC).
- Zero-byte files.
- Non-ASCII characters in path.
- The `_HEVC` suffix isn't consistent. Handle both 
  `IMG_NNNN.MOV` and `IMG_NNNN_HEVC.MOV`.

## Deliverables

1. `ingest.py` — main script
2. `requirements.txt` — pip deps
3. `README.md` — how to run, expected runtime, query examples
4. `queries.sql` — sanity-check queries:
   - Count by year
   - Find biggest files
   - Find Live Photo pairs
   - Find shared album items
   - Find items with no GPS
   - Find potential burst clusters (3+ items within 5s at same GPS)

## Out of Scope (Later)

- CLIP embeddings (Gallery will handle these)
- Face recognition
- Actual culling decisions
- iCloud deletion

## Testing Strategy

Test on small directory first:
```bash
python ingest.py --root $STORAGE_ROOT/photos/originals/2002  # 2.1MB
```
Verify schema and sample rows. Then:
```bash
python ingest.py --root $STORAGE_ROOT/photos/originals/2014  # 22GB
```
Only run on full root once confident.

## Constraints

- Limited RAM (8GB total, expect ~5GB free during run)
- Don't load all 91k paths into memory at once
- Don't write SQLite DB to $STORAGE_ROOT
- Script will run for hours — must survive SSH disconnects via tmux

Build incrementally. Show me the schema and a minimal prototype first, 
test on $STORAGE_ROOT/photos/originals/2002, iterate from there.
