# `enrichment/inputs/` — Apple Photos enrichment inputs

These are the operator-supplied inputs to `build.py`. They are **data, not source**: every file here is
matched by the repository `.gitignore` (`*.csv`, `*.sqlite`, `*.db`) and is deliberately untracked.

They were consolidated here on **2026-08-07**. Previously they sat loose in `~` on **delta**, which stopped
working when Loupe moved to charlie on 2026-08-07 — the code came across, its inputs did not, so
`build.py` would have failed on a missing `--leo`/`--bridge`/`--scores` path. Keeping them beside the code
that consumes them is what prevents that recurring.

| File | Size | What it is | Regenerable from |
|---|---|---|---|
| `leo-copy.sqlite` | 104 M | Apple Photos search index ("leo"/psi) — 84,112 items, 212,798 lexicon rows. Supplies label/scene terms. | The Mac's Photos library |
| `photos-bridge.csv` | 41 M | UUID↔asset bridge export mapping Apple Photos identifiers to library files. | Mac-side export |
| `scores.csv` | 3.7 M | Per-asset aesthetic/quality scores from the Apple-side extract. | Mac-side export |

## Notes

- `build.py` copies `leo-copy.sqlite` to `/tmp` before reading it, so the file here is never locked or
  mutated (`common.py: temp_copy`).
- `PRAGMA quick_check` on `leo-copy.sqlite` fails with `no such tokenizer: LEONLTokenizer`. That is
  **expected** — the FTS table uses Apple's proprietary tokenizer, absent from stock SQLite. The base
  `items` and `lexicon` tables read normally, and the pipeline uses its own decode path rather than FTS.
- The pipeline's output, `apple-enrichment.db`, lives at the repository root, not here.

Provenance and the wider migration: `homelab-vault/sessions/2026-08-07-loupe-to-charlie.md`.
