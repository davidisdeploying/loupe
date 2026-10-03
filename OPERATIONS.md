# Loupe operations

## Ownership

- Source repository: `~/loupe`
- Web application: repository root, served by `loupe.service` on port 8000
- Pipeline source: `pipeline/`
- Preftool experiment: `aesthetic/preftool/`, served separately on port 8770
- Runtime and pipeline data: `/data/loupe/state`
- Photo originals: `$STORAGE_ROOT/photos`, backed by the `$STORAGE_ROOT` compatibility alias
- Shared Python runtime: `/data/loupe-venv`

## Runtime state

Loupe-owned SQLite databases, caches, run markers, logs, pipeline metadata, and
thumbnail/export state live under `/data/loupe/state`. The supported overrides are:

- `DATA_ROOT`: generated data and Loupe application state
- `LOUPE_STATE_DIR`: backup and ledger view of that same state root
- `LOUPE_PIPELINE_DIR`: pipeline source, defaulting to `pipeline/`
- `LIBRARY_ROOT`: original-media root
- `LOUPE_PYTHON`: ledger-snapshot interpreter
- `LOUPE_LEDGER_DIR`: ledger-snapshot destination

Do not move runtime databases until the service is stopped, a verified off-host
snapshot is current, and every consumer has been checked against the new paths.

## Services

Canonical unit sources live in `deploy/systemd/`. Installed copies live under
`~/.config/systemd/user/`.

- `loupe.service`
- `preftool.service`
- `loupe-ledger.service` / `loupe-ledger.timer`
- `loupe-data-backup.service` / `loupe-data-backup.timer`

## Write authorization (W23)

Being on the LAN no longer implies write authority. Two independent gates apply to
every `do_POST` route, in order:

1. `_write_authorized()` at the single `do_POST` dispatch point — the shared write
   token. Every write route inherits it, so a new POST branch is covered by
   construction rather than by remembering to add another check.
2. the existing per-route `_is_lan_peer()` check — socket-truth plus Cloudflare
   header rejection.

The token gate is layered on top of the LAN check, never instead of it, so a
Cloudflare/guest request still fails with `LAN only` even if it presents a valid
token. Loopback peers are exempt from the token so server-local tooling on Charlie
keeps working. Reads are untouched.

The secret lives at `write_token` in `/data/loupe/state/loupe-settings.json` (mode
`600`, outside Git). It is read per request, so arming and disarming take effect
immediately with no restart:

```sh
# read the current token (run on charlie; do not paste it into a synced vault)
/data/loupe-venv/bin/python -c \
  'import json;print(json.load(open("/data/loupe/state/loupe-settings.json"))["write_token"])'

# disarm — removes the key and restores plain LAN-trust behaviour
/data/loupe-venv/bin/python -c \
  'import json,os;S="/data/loupe/state/loupe-settings.json";d=json.load(open(S));d.pop("write_token",None);json.dump(d,open(S+".tmp","w"),indent=2);os.replace(S+".tmp",S)'
```

Owner devices enroll once: the client `fetch` wrapper (installed at the top of
`static/app.js` and in the `setup_page.py` script block) attaches the header to every
non-GET request, and on a `write token required` 403 it prompts once and stores the
token in that browser's `localStorage`. Clearing site data means re-entering it.

If a token leaks, replace the `write_token` value; every device then re-prompts on its
next write.

## Derived-data caches

`/data/loupe/state/cache/preview` holds on-demand <=2048px JPEGs built from the
originals. It is derived data: anything evicted is rebuilt on the next request at
the cost of one NAS read.

It is capped and evicted least-recently-used first, driven from `build_preview()`,
the only writer into it. `LOUPE_PREVIEW_CACHE_MAX_MB` sets the cap (default 2048);
`0` disables eviction. The cache is walked only once enough new bytes have
accumulated to matter, and evicted down to 90% of the cap. `/data` is mounted
`relatime`, so access time is recorded and drives the ordering, with mtime as the
fallback. Unlinking a file that is currently being served is safe -- the open file
descriptor keeps the data readable until the response finishes.

Thumbnails, face crops, and video transcodes are separate caches and are not
currently capped.

## Logs

`loupe.service` appends both stdout and stderr to
`/data/loupe/state/logs/server.log`, so in-handler exceptions and cache/eviction
notices land in a file rather than vanishing. HTTP access logging remains disabled
(`log_message` is a no-op).

## Determinism baselines

`deploy/locks/` records what is actually installed, so drift is detectable:

- `loupe-venv.lock` — the resolved package set of `/data/loupe-venv` (Python 3.12).
  Descriptive, not a constraints file; regenerate with
  `/data/loupe-venv/bin/pip list --format=freeze`.
- `buffalo_l.sha256` — SHA-256 of the five insightface `buffalo_l` ONNX files.

`faces_pipeline.py` sets `MODEL_ROOT = HERE/.insightface`, so the **loaded** models are
`~/loupe/.insightface/models/buffalo_l`. `/data/loupe-insightface/` holds a second,
byte-identical copy left by the 2026-08-07 host move. Nothing sets `INSIGHTFACE_HOME`,
so it is inert — but it is 326 MB and, more importantly, a silent divergence risk if
either copy is ever updated alone. Retire it or make it the single source; do not leave
two copies indefinitely.

`.faces-venv` and `.stage5-query-venv` no longer exist; `/data/loupe-venv` is the only
interpreter.

## Quality harness

    tests/run.sh              # 34 tests, ~0.1s
    tests/run.sh static       # source-structure only; no service, no NAS
    tests/mutate.py           # tests the tests

stdlib `unittest` only, deliberately: `/data/loupe-venv` is the production
interpreter and adding pytest there would change `deploy/locks/loupe-venv.lock`.

Four groups. `test_static_invariants` parses `server.py` with `ast` rather than
importing it (importing opens databases and pulls in the ML stack) and asserts the
structural properties the security design rests on -- the write gate is the *first*
statement of `do_POST`, no POST route skips `_is_lan_peer`, the token compare is
constant-time, preview writes still trigger eviction. `test_data_invariants` checks
faithful-library rules against synthetic fixtures, including that `EXCLUDE_SQL` drops
no asset. `test_api_contract` runs the whole P2.2 security matrix against the live
service. `test_backup_rail` asserts a recent file exists in the ledger destination.

`test_dependencies` eagerly imports everything the runtime imports *lazily*. Both
outages found on 2026-08-09 were lazy-import failures the service survived at startup:
`sqlite_vec` (three days of silently failed ledger snapshots) and `ftfy` (`/api/search`
returning nothing at all). `test_embedding_golden` pins the query-side vectors so a
refactor cannot silently drift search quality -- opt-in via `LOUPE_TEST_SLOW=1`.

`test_schema` fingerprints all 12 databases and fails with a readable object-level
diff when one drifts. Schema versioning is recorded *beside* the databases rather
than in them: writing a `schema_version` row would mean migrating eleven live stores
holding irreproducible judgment, for the same drift detection. Bless a deliberate
change with `tests/capture_schema.py`.

`tests/mutate.py` breaks one invariant at a time in a copy of `server.py` and asserts
the matching test fails. Run it after editing the suite: it caught a test that was
matching `compare_digest` in a docstring and therefore passed even with the
constant-time compare removed.

## Query-side embedding modules

`stage5/text_embed_cpu.py` and `stage5/ort_env_cpu.py` were named `*_delta` until
2026-08-09 (W16). The query side has not run on delta since 2026-08-07, and the old
name implied a hardware limitation -- delta had no CUDA -- where the actual reason for
staying on CPU is resource policy: a single 64-token text query does not justify taking
the one GPU-0 resident-model slot Loupe shares with Ollama, Gallery ML and the vault
indexer. Behaviour is unchanged; the rename was verified byte-identical against
`tests/embed_golden.json`.

`ftfy` is a hard runtime dependency of the query path and is imported lazily. It went
missing from `/data/loupe-venv` during the host move and took `/api/search` down from
2026-08-07 to 2026-08-09 without any startup error. `tests/run.sh deps` now catches this
class of failure.

## Backup freshness

    tools/ledger_snapshot.sh --check-fresh [hours]   # default 36; exit 1 if stale

Checks that a recent snapshot **exists in the destination**, independent of any
unit's exit status -- which is the thing that lied during the 2026-08-06 to
2026-08-09 gap. `snapshot()` now also proves its own output landed and is
non-trivially sized before reporting success, and pointing `LOUPE_LEDGER_DIR` at
scratch prints a warning, because a snapshot in `/tmp` is not a backup.

## Portability (P7)

The media root is resolved from `LIBRARY_ROOT`, defaulting to `$STORAGE_ROOT/photos`. As of
2026-08-09 the pipeline follows the same scheme rather than spelling the mount out:

- `pipeline/culling.py` derives its work-product exclusion from `LIBRARY_ROOT`. `server.py`
  builds the same "production" prefix from the same variable. Two hand-written copies of
  one concept disagree the moment the root moves, so `tests/test_portability.py` asserts
  they still describe the same tree.
- `pipeline/make_review_sheets.py` strips the configured root, not one host's mount point.
- `stage5/recipe_siglip2.py` keeps a stored-to-local path translation, now configurable via
  `LOUPE_STORED_PATH_PREFIX` / `LOUPE_LOCAL_PATH_PREFIX`. Since the host move it is inert:
  charlie carries `$STORAGE_ROOT -> $STORAGE_ROOT/the compute host`, so both prefixes name the same tree and
  stored filepaths were deliberately never rewritten. It remains for a host without that
  symlink.

`tests/run.sh port` fails on any new hard-coded mount point outside the env scheme. The
allowlist is short and each entry carries its reason; the `pipeline/culling/` one-shot
migrations are on it deliberately, because de-hardcoding a script that already ran and
must never run again would imply it is re-runnable.

## Apple enrichment: reproducible, not portable (W17)

`apple-enrichment.db` (77,684 assets, 1.19 M labels, 35,564 person rows, 77,684 Apple
aesthetic scores) is **reproducible** — `enrichment/build.py` is a parameterised builder,
every path an argument with an env-derived default, and the database carries a
`provenance` table naming each input. `enrichment/README.md` documents the run. The
extraction inputs are preserved in `enrichment/inputs/`.

It is **not portable**. Those inputs came from a specific Apple Photos library; a second
user has no equivalent and cannot regenerate this database at all. Treat Apple enrichment
as owner-only data, not a product feature — the portable replacement is the aesthetic MLP
head (W18), which distils the SigLIP2 embeddings against these scores and is the actual
second-user gate.

## Two backup paths, and why both matter

| path | what | survives | destination |
|---|---|---|---|
| `tools/ledger_snapshot.sh` | consistent tarball of the 11 sidecar databases, daily | losing Charlie | `$STORAGE_ROOT/loupe-ledger` |
| `deploy/loupe_data_backup.py` | per-database mirror, daily | **losing the NAS** | delta `~/FleetDatabaseBackups/charlie-snapshots` |

Neither subsumes the other, and they had drifted. Until 2026-08-09 `vault.db`, `edits.db`,
`renders.db`, `pairs.db` and `summaries.db` were in the ledger but not the off-host
mirror, so their only copy outside Charlie was a tarball on the NAS. `vault.db` and
`edits.db` are vault marks and edits -- direct human decisions that no pipeline re-run
reproduces. Added and verified on delta: `quick_check ok`, row counts matching live.

`tests/run.sh cover` fails if a ledger database ever lacks an off-host home again.

The ledger *tarball directory* itself still has no offsite copy. That matters much less
now that every database inside it is independently mirrored, but it is not nothing:
losing the NAS still costs the point-in-time consistency the tarball provides.

## Stage liveness (W14)

    DATA_ROOT=/data/loupe/state python3 run_control.py --check-stalled [idle_seconds]

Exit 0 clean, 1 if a stage is stalled or crashed, **2 if it could not find its markers**.

A stage marker records `state`, `pid` and `started_at` — nothing that advances — so a
wedged stage and a working one look identical. The stage log is already an append-only
progress stream, so its mtime is the heartbeat: a `running` marker whose log has not
grown in `LOUPE_STAGE_STALL_SECONDS` (default 1800) is stalled.

`stalled` and `crashed` are kept apart deliberately. A live pid with a frozen log is
wedged work; a dead pid with a `running` marker means the runner died without recording
it, so the marker is lying and waiting will never help.

**`DATA_ROOT` must be set.** Without it the run-status directory resolves under the repo
instead of the state root, and the check would find no markers, report every stage idle
and exit 0 — reassuring precisely because it is looking at nothing. It refuses with exit
2 instead. This is the same failure shape as the ledger unit reporting SUCCESS for three
days while producing no snapshot; the rule is that a check which cannot see its subject
must never pass.

## Seeing the UI

    tools/shoot.sh                 # / at 1440 and 640
    tools/shoot.sh /setup /map     # named routes

Renders Loupe headlessly and writes PNGs to `/tmp/loupe-shots`. Run it from a host with
Chrome -- the Mac, pointed at the Tailscale address. Charlie has no browser and
puppeteer's Chrome download fails there.

Every remaining design phase (audit Parts 8-9) replaces a working surface. Shipping a
layout change nobody can look at is guessing, not caution.

**Screenshots stay out of the vault.** They contain faces and `~/Vaults` replicates to
every host on the mesh. `/tmp` is the right home for them; copy one into
`loupe-vault/files/` only deliberately, and only if it is worth keeping.

## Cross-host dependencies

- Alpha's Edge tunnel publishes the Charlie origin.
- Nexus on Alpha probes Loupe, its database freshness, and Charlie GPU health.
- The pipeline and full-image routes depend on the Echo NAS mount.
- Loupe shares Charlie GPU resources with Ollama, Gallery, the vault indexer, and
  explicitly scheduled ffmpeg work.

Only one GPU-heavy or NAS-heavy job may be active at a time.
