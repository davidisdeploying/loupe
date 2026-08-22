-- ============================================================================
-- queries.sql  —  photo-library culling candidate rules
-- Generated 2026-06-15.  Source DB: metadata.db (91,537 rows).
-- Backup snapshot taken first: ~/loupe-archive/metadata-backups/metadata-backup-20260615-0327.db
--
-- READ THIS BEFORE TOUCHING ANYTHING DESTRUCTIVE:
--   * Every rule below is a *candidate* generator. Nothing here deletes.
--   * Every rule EXCLUDES the protected Ray-Ban Meta glasses set (see below).
--
-- PROTECTED SET — Ray-Ban Meta glasses footage (NEVER a delete candidate)
--   The brief said "use the exact camera_model string 'Ray-Ban Meta Smart
--   Glasses'". That string matches only 3,744 rows. But the SAME footage also
--   lands under camera_model NULL / 'HSTN' / '2Q37S...' (EXIF stripped or
--   variant model id) while keeping the unmistakable Meta export filename
--   signature '%_singular_display%'. That is another 1,470 rows / ~74 GB.
--   To honour "NEVER a delete candidate", the protected set is the UNION:
--       camera_model = 'Ray-Ban Meta Smart Glasses'  OR  filename LIKE '%_singular_display%'
--   = 5,214 rows / 245.72 GB.  (All 3,744 model-matched rows are also
--   _singular_display-named, so the union == the _singular_display set.)
--
-- NULL-SAFETY WARNING (important):
--   Do NOT exclude with  AND NOT (camera_model = 'Ray-Ban Meta Smart Glasses').
--   27,030 rows have camera_model IS NULL; for them  camera_model = 'x'  is NULL,
--   so NOT(NULL) is NULL and the row is silently DROPPED from the result. That
--   bug alone turned "junk imports" from 4,624 rows into 0. We therefore define
--   a TEMP VIEW of protected ids and exclude with  id NOT IN (SELECT id FROM protected),
--   which is null-safe.
-- ============================================================================

-- Run this whole file with:  sqlite3 metadata.db < queries.sql
-- (culling.py creates the same view in Python; this view is for ad-hoc use.)

-- Two SEPARATE protected categories, folded into ONE null-safe exclusion view.
--   glasses     — Ray-Ban Meta footage (camera_model / naming signature)
--   workproduct — out-of-scope work-product, protected BY PATH on purpose
--                 (rendered exports with NULL EXIF/GPS/camera/duration that were
--                  otherwise misclassified as junk). Path is the discriminator.
--   protected   — the union; every rule excludes via  id NOT IN (SELECT id FROM protected).
DROP VIEW IF EXISTS glasses;
DROP VIEW IF EXISTS workproduct;
DROP VIEW IF EXISTS protected;
CREATE TEMP VIEW glasses AS
    SELECT id FROM assets
    WHERE camera_model = 'Ray-Ban Meta Smart Glasses'
       OR filename LIKE '%\_singular\_display%' ESCAPE '\';
CREATE TEMP VIEW workproduct AS
    SELECT id FROM assets
    WHERE filepath LIKE '/mnt/nas/photos/production/%'
       OR filepath LIKE '/mnt/nas/photos/long-video-elsewhere/%';
CREATE TEMP VIEW protected AS
    SELECT id FROM glasses UNION SELECT id FROM workproduct;

-- Sanity: glasses 5214, workproduct 358, protected union 5572
SELECT '-- protected glasses rows: '     || COUNT(*) FROM glasses;
SELECT '-- protected workproduct rows: ' || COUNT(*) FROM workproduct;
SELECT '-- protected union rows: '       || COUNT(*) FROM protected;


-- ============================================================================
-- TIER A  —  high-confidence, auto-delete candidates
-- ============================================================================

-- ----------------------------------------------------------------------------
-- A1. Exact SHA-256 duplicates.
--     Group by content hash; keep ONE copy, list the rest as candidates.
--     Keep-preference (best copy survives):
--        1. has a real capture_timestamp   (capture_timestamp IS NOT NULL)
--        2. lives in a dated folder         (filepath matches /YYYY/MM/)
--        3. lowest id (oldest ingest)       (tie-breaker, deterministic)
--     NOTE: on this DB there are currently 0 hash-dup groups, so this returns
--     nothing — but the rule is written correctly for when dupes appear.
-- ----------------------------------------------------------------------------
WITH dups AS (
    SELECT file_sha256
    FROM assets
    WHERE id NOT IN (SELECT id FROM protected)
      AND file_sha256 IS NOT NULL
    GROUP BY file_sha256
    HAVING COUNT(*) > 1
),
ranked AS (
    SELECT a.*,
           ROW_NUMBER() OVER (
               PARTITION BY a.file_sha256
               ORDER BY
                   (a.capture_timestamp IS NOT NULL) DESC,                      -- real timestamp wins
                   (a.filepath GLOB '*/[0-9][0-9][0-9][0-9]/[0-9][0-9]/*') DESC,-- dated folder wins
                   a.id ASC                                                     -- oldest ingest wins
           ) AS keep_rank
    FROM assets a
    JOIN dups d ON d.file_sha256 = a.file_sha256
    WHERE a.id NOT IN (SELECT id FROM protected)
)
SELECT id, filepath, filename, file_sha256, file_size_bytes,
       capture_timestamp, year, month,
       'A1_sha256_dupe' AS rule
FROM ranked
WHERE keep_rank > 1            -- keep_rank = 1 is the survivor; everything else is a candidate
ORDER BY file_sha256, keep_rank;

-- ----------------------------------------------------------------------------
-- A2. Orphan short MOVs — SPLIT BY DURATION (the old single <3s bucket was too
--     blunt: ~78% sat in the 2-3s band where standalone clips are often
--     intentional, not fumbles, so they must not be auto-delete).
--
--     NOTE on duration coverage: duration_seconds is NOT 100% NULL. It is
--     populated for videos (35,130 of 35,133 video rows have it; only NULL on
--     non-video rows + 1 stray MOV). So these predicates are real, not empty —
--     the combined <3s set is 3,841 rows / 6.41 GB on this DB. NULL durations
--     are excluded by design (NULL < n evaluates to unknown -> filtered out).
--
--   A2a — TIER A (auto-delete candidate): truly accidental sub-second taps.
-- ----------------------------------------------------------------------------
SELECT id, filepath, filename, file_size_bytes, duration_seconds,
       capture_timestamp, year, month,
       'A2a_orphan_mov_sub1s' AS rule
FROM assets
WHERE id NOT IN (SELECT id FROM protected)
  AND extension = 'MOV'
  AND is_live_photo_video = 0
  AND duration_seconds < 1            -- ~154 rows; NULL durations excluded (NULL<1 is unknown)
ORDER BY duration_seconds, file_size_bytes DESC;

-- ----------------------------------------------------------------------------
--   A2b — TIER B (human review): 1-3s standalone clips. Often intentional short
--     recordings, so reviewed rather than auto-deleted. (~3,687 rows.)
-- ----------------------------------------------------------------------------
SELECT id, filepath, filename, file_size_bytes, duration_seconds,
       capture_timestamp, year, month,
       'A2b_orphan_mov_1to3s' AS rule
FROM assets
WHERE id NOT IN (SELECT id FROM protected)
  AND extension = 'MOV'
  AND is_live_photo_video = 0
  AND duration_seconds >= 1 AND duration_seconds < 3
ORDER BY duration_seconds, file_size_bytes DESC;


-- ============================================================================
-- TIER B  —  needs human review
-- ============================================================================

-- ----------------------------------------------------------------------------
-- B1. Genuinely-shared content.
--     (is_shared_album=1 OR filename LIKE 'od_%' OR filename LIKE '%_singular_display%')
--     AND not in the protected glasses set.
--
--     RESULT ON THIS DB: 0 rows.  The "shared" signal collapses to EMPTY once
--     glasses footage is removed. We verified is_shared_album=1 is EXACTLY the
--     _singular_display set (5,214 == 5,214, zero symmetric difference), and the
--     od_ / mcp_ prefixes occur ONLY on _singular_display files. In other words
--     there is NO genuine shared-album content in this library — every "shared"
--     flag was set by the Ray-Ban Meta import. This bucket is effectively dead.
-- ----------------------------------------------------------------------------
SELECT id, filepath, filename, is_shared_album, camera_model,
       year, month,
       'B1_shared' AS rule
FROM assets
WHERE id NOT IN (SELECT id FROM protected)
  AND (is_shared_album = 1
       OR filename LIKE 'od\_%' ESCAPE '\'
       OR filename LIKE '%\_singular\_display%' ESCAPE '\')
ORDER BY year, month;

-- ----------------------------------------------------------------------------
-- B2. Screenshots — PNG at iPhone screen resolutions (either orientation).
--     iPad screenshots (e.g. 2388x1668 / 1668x2388) are deliberately EXCLUDED
--     per the brief ("iPhone screen resolutions"); they are reported separately
--     in culling.py as a flag, not as candidates.
--     Broken out by year in the CSV / summary.
-- ----------------------------------------------------------------------------
SELECT id, filepath, filename, width_pixels, height_pixels,
       year, month, file_size_bytes,
       'B2_screenshot' AS rule
FROM assets
WHERE id NOT IN (SELECT id FROM protected)
  AND extension = 'PNG'
  AND (
        -- portrait WxH (and landscape HxW) for the iPhone line, oldest -> newest
        (width_pixels= 640 AND height_pixels=1136) OR (width_pixels=1136 AND height_pixels= 640) OR -- 5/5s/SE1
        (width_pixels= 750 AND height_pixels=1334) OR (width_pixels=1334 AND height_pixels= 750) OR -- 6/7/8/SE2/3
        (width_pixels= 828 AND height_pixels=1792) OR (width_pixels=1792 AND height_pixels= 828) OR -- XR/11
        (width_pixels=1080 AND height_pixels=1920) OR (width_pixels=1920 AND height_pixels=1080) OR -- Plus (rendered)
        (width_pixels=1242 AND height_pixels=2208) OR (width_pixels=2208 AND height_pixels=1242) OR -- Plus (native)
        (width_pixels=1125 AND height_pixels=2436) OR (width_pixels=2436 AND height_pixels=1125) OR -- X/XS/11Pro/12-13mini
        (width_pixels=1170 AND height_pixels=2532) OR (width_pixels=2532 AND height_pixels=1170) OR -- 12/13/14
        (width_pixels=1179 AND height_pixels=2556) OR (width_pixels=2556 AND height_pixels=1179) OR -- 14Pro/15/15Pro/16
        (width_pixels=1206 AND height_pixels=2622) OR (width_pixels=2622 AND height_pixels=1206) OR -- 16 Pro
        (width_pixels=1242 AND height_pixels=2688) OR (width_pixels=2688 AND height_pixels=1242) OR -- XSMax/11ProMax
        (width_pixels=1284 AND height_pixels=2778) OR (width_pixels=2778 AND height_pixels=1284) OR -- 12-13ProMax/14Plus
        (width_pixels=1290 AND height_pixels=2796) OR (width_pixels=2796 AND height_pixels=1290) OR -- 14ProMax/15+/16+
        (width_pixels=1320 AND height_pixels=2868) OR (width_pixels=2868 AND height_pixels=1320)    -- 16 Pro Max
      )
ORDER BY year, month;

-- ----------------------------------------------------------------------------
-- B3. Burst clusters — 3+ frames within 5s at the same GPS (rounded ~4 dp).
--     Within each cluster KEEP the 1-3 sharpest by blur_laplacian; list the rest.
--
--     SQL caveat: true "within 5s of each other" is a sequential/gap problem
--     (rows can chain past any fixed bucket boundary). culling.py does the exact
--     gap-based sequential clustering. The query below is the SQL-expressible
--     APPROXIMATION used for documentation / spot-checking: bucket capture_time
--     into 5-second windows per rounded-GPS cell and flag cells with >=3 frames.
--     Trust culling.py's B3_burst_extras.csv over this query.
-- ----------------------------------------------------------------------------
WITH eligible AS (
    SELECT id, filepath, filename, blur_laplacian, file_size_bytes,
           capture_timestamp, year, month,
           ROUND(gps_lat, 4) AS glat,
           ROUND(gps_lon, 4) AS glon,
           CAST(capture_timestamp / 5 AS INTEGER) AS tbucket
    FROM assets
    WHERE id NOT IN (SELECT id FROM protected)
      AND capture_timestamp IS NOT NULL
      AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL
),
clusters AS (
    SELECT glat, glon, tbucket, COUNT(*) AS cnt
    FROM eligible
    GROUP BY glat, glon, tbucket
    HAVING COUNT(*) >= 3
),
ranked AS (
    SELECT e.*,
           ROW_NUMBER() OVER (
               PARTITION BY e.glat, e.glon, e.tbucket
               ORDER BY e.blur_laplacian DESC      -- sharpest first; NULL blur sorts last
           ) AS sharp_rank
    FROM eligible e
    JOIN clusters c
      ON c.glat = e.glat AND c.glon = e.glon AND c.tbucket = e.tbucket
)
SELECT id, filepath, filename, blur_laplacian, file_size_bytes,
       glat, glon, tbucket, sharp_rank,
       'B3_burst_extra' AS rule
FROM ranked
WHERE sharp_rank > 3        -- keep the 3 sharpest; the rest are candidates
ORDER BY glat, glon, tbucket, sharp_rank;

-- ----------------------------------------------------------------------------
-- B4. Blurry images.  Threshold is NOT hardcoded here — see culling.py, which
--     prints the blur_laplacian distribution and applies a data-driven cut
--     (default = 10th percentile) AND tags each candidate with in_burst so you
--     can compare blurry-in-burst vs isolated-blurry. For reference, percentiles
--     on the 55,954 non-protected images with a blur score:
--        p1=26.8  p5=61.7  p10=93.6  p25=207.3  p50=530.0  p75=1227.5  p90=2414.0
--     The literal below is the p10 value at generation time; culling.py
--     recomputes it from live data each run.
-- ----------------------------------------------------------------------------
SELECT id, filepath, filename, blur_laplacian, file_size_bytes,
       capture_timestamp, gps_lat, gps_lon, year, month,
       'B4_blurry' AS rule
FROM assets
WHERE id NOT IN (SELECT id FROM protected)
  AND blur_laplacian IS NOT NULL
  AND blur_laplacian < 93.6        -- p10; culling.py recomputes this from live data
ORDER BY blur_laplacian ASC;

-- ----------------------------------------------------------------------------
-- B5. Junk imports — no EXIF capture time, no GPS, no camera make, no model.
--     Typically web downloads / screenshots-of-screenshots / saved memes.
--     (~4,624 rows / ~8.3 GB on this DB.)  Reminder: these all have
--     camera_model IS NULL, which is exactly why the null-safe exclusion matters.
-- ----------------------------------------------------------------------------
SELECT id, filepath, filename, extension, file_size_bytes, year, month,
       'B5_junk_import' AS rule
FROM assets
WHERE id NOT IN (SELECT id FROM protected)
  AND capture_timestamp IS NULL
  AND gps_lat IS NULL
  AND camera_make IS NULL
  AND camera_model IS NULL
ORDER BY file_size_bytes DESC;
