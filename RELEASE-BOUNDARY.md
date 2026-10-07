# Release boundary — what ships, what is yours, what a second install builds

P7. Written 2026-08-09 from the live install, not from intent. Every count is measured.

Loupe is meant to be installable by someone who is not the author (a second person is the test user).
That only works if three things are cleanly separated: the **product**, the **owner's
data**, and the **derived state a fresh install regenerates for itself**.

## 1. What ships — 130 files, 2.0 MB

Everything tracked in git: application and pipeline source, `static/` (CSS, JS, brand
SVGs, icons, `basemap.geojson`), `deploy/` systemd units and `deploy/locks/`, `tests/`,
`enrichment/` source, and the Markdown docs.

Nothing tracked contains a credential, a person's name, or a photograph. Verified rather
than asserted: `secrets.env` and `loupe-settings.json` are both gitignored and untracked,
and `tests/test_release_boundary.py` fails if that stops being true.

## 2. What never ships — the owner's data

| thing | where | why it cannot ship |
|---|---|---|
| the photo library | `$STORAGE_ROOT/photos` | it is the owner's photographs |
| `loupe-settings.json` | state root, mode 600 | residences, protected-people names, the W23 write token |
| `secrets.env` | repo root, mode 600 | credentials |
| `apple-enrichment.db` + `enrichment/inputs/` | state root / repo | extracted from one specific Apple Photos library — see W17 |
| thumbnails, previews, transcodes, face crops | state root | derived from the library; regenerate |
| `.insightface/` model weights | repo dir | 601 MB of vendor weights; fetch, do not ship |

`PROTECTED_NAMES` ships **empty** on purpose; real names live only in the gitignored
settings file.

## 3. Sidecars — the part that matters

The audit's phrase is "hundreds of hours of irreproducible human judgment". Measured:

### Tier 1 — human decisions. Never regenerable. Must be backed up.

| store | rows | what it is |
|---|---|---|
| `decisions.db` → `decisions` | 248 | keep / cut calls |
| `vault.db` → `vault` | 205 | vault marks |
| `faces.db` → `persons` | 20 | names the owner typed |
| `faces.db` → `assignments` | 35,077 | face → person |
| `faces.db` → `rejections` | 24 | explicit "not this person" |

**`faces.db` is mixed, and this is the hazard.** The same 703 MB file holds 170,082
ML-derived face rows *and* 35,121 human decisions. "Rebuild the face pipeline" must never
be executed as "delete `faces.db` and re-run" — that silently destroys the person graph
while looking like a clean rebuild. Any rebuild has to preserve `persons`, `assignments`
and `rejections`.

### Tier 2 — owner-only, reproducible only by this owner

`apple-enrichment.db` (77,684 scored assets, 1,186,205 labels, 35,564 person rows).
Reproducible via `enrichment/build.py` from preserved inputs, but a second user has no
equivalent inputs at all. The portable replacement is the aesthetic MLP head (W18).

### Tier 3 — derived. A fresh install builds these for itself.

`metadata.db` (102,678 assets, from ingest), `faces.db` → `faces`/`processed`,
`clusters.db`, `nsfw.db`, `pairs.db`, `renders.db`, `summaries.db`,
`stage5/embeddings_siglip2.db`. All rebuildable from the originals plus the shipped code,
given time and a GPU.

## 4. What a second install has to do

1. Point `LIBRARY_ROOT` at its own originals and `DATA_ROOT` at its own state root.
2. Fetch model weights (`buffalo_l`, SigLIP2 ONNX) — not shipped; hashes in
   `deploy/locks/buffalo_l.sha256` for verification.
3. Create its own `loupe-settings.json`; set a W23 write token if the LAN is not trusted.
4. Run the pipeline: ingest → thumbnails → faces → nsfw → embeddings.
5. Skip Apple enrichment entirely. It is Tier 2 and does not apply.

Nothing in steps 1–5 requires anything from David's install.

## 5. Known boundary debt

- `pipeline/video/` is vendored verbatim and still carries hard-coded `$STORAGE_ROOT` paths;
  allowlisted in `tests/test_portability.py`, tracked in that directory's README.
- `pipeline/culling/` one-shot migrations carry this house's paths. Historical; they
  should not run on a second install at all.
- Model weights have no fetch script — a second install currently obtains them by hand.
