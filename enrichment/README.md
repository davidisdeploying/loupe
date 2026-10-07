# enrichment/ — reproducible builder for `apple-enrichment.db`

Committed, parameterized form of the one-off that originally produced `apple-enrichment.db`
(it lived only as `/tmp/build_enrichment.py` + a loose scores-load step). Turns the Apple-Photos
extraction inputs into a single database keyed to `metadata.db.assets.id`, left-joining four
signals onto a UUID bridge:

```
bridge (uuid↔asset_id)  →  labels (version-aware)  +  persons  +  apple aesthetic score
```

| File | Role |
|---|---|
| `common.py` | the recipe: bridge-CSV parse, the filename+timestamp matcher (25 h tz window, `_HEVC`→still mapping, confidence tiers, refuse-rather-than-misstamp), version-aware labels (osxphotos API → leo direct-decode fallback), persons (named only), scores (osxphotos API → `scores.csv`), schema + writer. |
| `build.py` | CLI orchestrator. `--bridge … [--library … | --leo … --scores …] --out …` |

## Run

```bash
# Linux / beta-macOS extract (leo-decode + scores.csv).
# Inputs live in `enrichment/inputs/` (untracked data) -- see its README:
python3 build.py --bridge enrichment/inputs/photos-bridge.csv \
    --leo enrichment/inputs/leo-copy.sqlite \
    --scores enrichment/inputs/scores.csv \
    --out /tmp/apple-enrichment.rebuilt.db

# stable macOS (osxphotos API for labels + scores):
python3 build.py --bridge bridge.csv --library "~/Pictures/Photos Library.photoslibrary" \
    --out apple-enrichment.new.db
```

## Discipline

- `metadata.db` is opened **read-only**.
- the leo/psi search index is worked on a **`/tmp` copy** (source never touched/locked).
- `--out` is a **new file** and the builder **refuses to overwrite** an existing one — the live
  `apple-enrichment.db` is never clobbered. Promote with `mv` once the counts check out.

## Verified reproduction

Rebuilt from the original loose inputs, the output is **bit-for-bit identical** to the live DB:
`asset_uuid` 73,157 · `labels` 1,152,875 rows / 71,750 assets · `persons` 31,433 / 24,619 ·
`apple_score` 73,157 — and the per-method bridge breakdown matches exactly. See `../ONBOARDING.md`
for the end-to-end sequence.
