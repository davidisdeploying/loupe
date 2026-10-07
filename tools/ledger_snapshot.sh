#!/usr/bin/env bash
# Consistent ledger snapshot of loupe sidecar DBs -> $HOME/loupe-archive/loupe-ledger.
# Usage: ledger_snapshot.sh              take a snapshot, prune old ones
#        ledger_snapshot.sh --verify [tarball]   verify a tarball (latest if omitted)
#        ledger_snapshot.sh --check-fresh [hours] assert a recent snapshot EXISTS (default 36)
#
# 2026-08-09: the Aug 7/8 runs failed on a missing sqlite-vec extension while a manual
# run with LOUPE_LEDGER_DIR pointed at /tmp left `systemctl status` reporting SUCCESS.
# Three days passed with no NAS snapshot and nothing looked wrong. Hence: snapshot()
# now proves its own output landed in a durable destination, and --check-fresh exists
# so freshness can be asserted without taking a snapshot.
set -euo pipefail

LOUPE_DIR="$HOME/loupe"
LOUPE_STATE_DIR="${LOUPE_STATE_DIR:-${DATA_ROOT:-$LOUPE_DIR}}"
EMBEDDINGS_DB="${LOUPE_EMBEDDINGS_DB:-$LOUPE_DIR/stage5/embeddings_siglip2.db}"
NAS_DIR="${LOUPE_LEDGER_DIR:-$HOME/loupe-archive/loupe-ledger}"
# A snapshot written to scratch is not a backup. Overriding to /tmp is legitimate for a
# one-off verify, but it must be loud rather than silently look like a successful run.
case "$NAS_DIR" in
  /tmp/*|/var/tmp/*|/dev/shm/*)
    echo "WARNING: ledger destination is scratch ($NAS_DIR) — this is NOT a backup" >&2 ;;
esac
RETAIN=14
PYTHON_BIN="${LOUPE_PYTHON:-/data/loupe-venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Loupe Python unavailable: $PYTHON_BIN" >&2
  exit 2
worker1

DBS=(
  decisions.db
  vault.db
  edits.db
  renders.db
  faces.db
  nsfw.db
  clusters.db
  pairs.db
  summaries.db
  apple-enrichment.db
  stage5/embeddings_siglip2.db
)

usage() {
  echo "Usage: $(basename "$0") [--verify [tarball]]" >&2
  exit 1
}

utc_stamp() { date -u '+%Y%m%d-%H%M%S'; }

db_source() {
  case "$1" in
    stage5/embeddings_siglip2.db) printf '%s\n' "$EMBEDDINGS_DB" ;;
    *) printf '%s\n' "$LOUPE_STATE_DIR/$1" ;;
  esac
}

backup_db() {
  # Consistent copy via sqlite backup() API -- never a raw cp of a hot WAL db.
  local src="$1" dst="$2"
  "$PYTHON_BIN" - "$src" "$dst" <<'PY'
import sqlite3, sys
src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(f'file:{src_path}?mode=ro', uri=True)
dst = sqlite3.connect(dst_path)
src.backup(dst)
# Fold any inherited WAL into the main file so the copy is a single
# self-contained artifact -- no dangling -wal/-shm sidecars in the tarball.
dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
dst.execute('PRAGMA journal_mode=DELETE')
dst.close()
src.close()
PY
}

write_manifest() {
  local snap_dir="$1" manifest="$2"
  local utc central head
  utc=$(date -u '+%Y-%m-%d %H:%M UTC')
  central=$(TZ=America/Chicago date '+%H:%M %Z')
  head=$(git -C "$LOUPE_DIR" rev-parse HEAD)

  "$PYTHON_BIN" - "$snap_dir" "$manifest" "$utc" "$central" "$head" "${DBS[@]}" <<'PY'
import hashlib, json, os, sqlite3, sys

snap_dir, manifest_path, utc, central, head = sys.argv[1:6]
rel_dbs = sys.argv[6:]

try:
    import sqlite_vec
    HAVE_VEC = True
except ImportError:
    HAVE_VEC = False

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def table_counts(path):
    con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    if HAVE_VEC:
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
    counts = {}
    for (name,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        counts[name] = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    con.close()
    return counts

dbs = {}
for rel in rel_dbs:
    path = os.path.join(snap_dir, rel)
    dbs[rel] = {
        'sha256': sha256_of(path),
        'tables': table_counts(path),
    }

manifest = {
    'timestamp_utc': utc,
    'timestamp_central': central,
    'source_head': head,
    'dbs': dbs,
}

with open(manifest_path, 'w') as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
    f.write('\n')
PY
}

prune() {
  local tarballs count
  mapfile -t tarballs < <(ls -1t "$NAS_DIR"/ledger-*.tar.zst 2>/dev/null || true)
  count=${#tarballs[@]}
  if (( count > RETAIN )); then
    for ((i=RETAIN; i<count; i++)); do
      echo "Pruning ${tarballs[$i]}"
      rm -f "${tarballs[$i]}"
    done
  worker1
}

snapshot() {
  local stamp tmpdir tarball
  stamp=$(utc_stamp)
  tmpdir=$(mktemp -d "/tmp/loupe-ledger-snap.XXXXXX")
  trap "rm -rf '$tmpdir'" EXIT

  mkdir -p "$NAS_DIR"

  local rel src dst
  for rel in "${DBS[@]}"; do
    src=$(db_source "$rel")
    dst="$tmpdir/$rel"
    mkdir -p "$(dirname "$dst")"
    backup_db "$src" "$dst"
  done

  write_manifest "$tmpdir" "$tmpdir/manifest.json"

  tarball="$NAS_DIR/ledger-${stamp}.tar.zst"
  tar -C "$tmpdir" --zstd -cf "$tarball" .

  # Prove the output actually landed before claiming success. tar can exit 0 having
  # written nothing useful (full filesystem, vanished mount), and an exit code that
  # outlives its artifact is exactly how the Aug 6-9 gap stayed invisible.
  if [[ ! -f "$tarball" ]]; then
    echo "FAILED: $tarball does not exist after tar" >&2
    exit 3
  worker1
  local size
  size=$(stat -c%s "$tarball")
  if (( size < 1048576 )); then
    echo "FAILED: $tarball is only ${size} bytes — refusing to call that a snapshot" >&2
    exit 3
  worker1
  echo "Wrote $tarball (${size} bytes)"

  prune
}

check_fresh() {
  # Assert a recent snapshot EXISTS in the destination. Deliberately independent of any
  # unit's exit status, because that is the thing that lied.
  local max_h="${1:-36}" newest age_s age_h
  newest=$(ls -1t "$NAS_DIR"/ledger-*.tar.zst 2>/dev/null | head -1 || true)
  if [[ -z "$newest" ]]; then
    echo "STALE: no snapshot in $NAS_DIR" >&2
    exit 1
  worker1
  age_s=$(( $(date +%s) - $(stat -c%Y "$newest") ))
  age_h=$(( age_s / 3600 ))
  if (( age_h >= max_h )); then
    echo "STALE: newest snapshot $(basename "$newest") is ${age_h}h old (limit ${max_h}h)" >&2
    exit 1
  worker1
  echo "FRESH: $(basename "$newest") is ${age_h}h old (limit ${max_h}h)"
}

verify() {
  local tarball="${1:-}" tmpdir
  if [[ -z "$tarball" ]]; then
    tarball=$(ls -1t "$NAS_DIR"/ledger-*.tar.zst 2>/dev/null | head -1 || true)
  worker1
  if [[ -z "$tarball" || ! -f "$tarball" ]]; then
    echo "No tarball found to verify" >&2
    exit 1
  worker1

  tmpdir=$(mktemp -d "/tmp/loupe-ledger-verify.XXXXXX")
  trap "rm -rf '$tmpdir'" EXIT
  tar -C "$tmpdir" --zstd -xf "$tarball"

  "$PYTHON_BIN" - "$tmpdir" <<'PY'
import json, sqlite3, sys, hashlib, os

snap_dir = sys.argv[1]
with open(os.path.join(snap_dir, 'manifest.json')) as f:
    manifest = json.load(f)

try:
    import sqlite_vec
    HAVE_VEC = True
except ImportError:
    HAVE_VEC = False

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def table_counts(path):
    con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    if HAVE_VEC:
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
    counts = {}
    for (name,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        counts[name] = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    con.close()
    return counts

ok = True
for rel, info in manifest['dbs'].items():
    path = os.path.join(snap_dir, rel)
    actual_sha = sha256_of(path)
    actual_counts = table_counts(path)
    if actual_sha != info['sha256'] or actual_counts != info['tables']:
        ok = False
        if actual_sha != info['sha256']:
            print(f'MISMATCH sha256 {rel}: manifest={info["sha256"]} actual={actual_sha}')
        if actual_counts != info['tables']:
            print(f'MISMATCH row counts {rel}: manifest={info["tables"]} actual={actual_counts}')
    else:
        print(f'OK {rel}: {sum(actual_counts.values())} rows across {len(actual_counts)} tables')

sys.exit(0 if ok else 1)
PY
}

case "${1:-}" in
  --verify) shift; verify "${1:-}" ;;
  --check-fresh) shift; check_fresh "${1:-36}" ;;
  "") snapshot ;;
  *) usage ;;
esac
