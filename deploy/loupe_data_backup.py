#!/usr/bin/env python3
"""Create verified, point-in-time backups of Alpha SQLite state.

Covers both control-plane databases and the application databases this host
gained during the 2026-08-06/07 consolidation. Delta is the off-host
destination (see control_plane_offhost_sync.py)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


LOUPE_STATE_DIR = Path(
    os.environ.get("LOUPE_STATE_DIR", str(Path.home() / "loupe"))
)


SOURCES = {
    # Loupe derived databases. The photo originals live on the NAS and are
    # mirrored to Google Drive; these are the *derived* products of the ML
    # pipeline -- asset metadata, face clusters, cull decisions, NSFW scoring,
    # SigLIP2 embeddings -- and after the 2026-08-07 move from delta they
    # existed on this host and nowhere else. Regenerating them means re-running
    # the whole pipeline.
    "loupe-metadata": LOUPE_STATE_DIR / "metadata.db",
    "loupe-faces": LOUPE_STATE_DIR / "faces.db",
    "loupe-decisions": LOUPE_STATE_DIR / "decisions.db",
    "loupe-clusters": LOUPE_STATE_DIR / "clusters.db",
    "loupe-nsfw": LOUPE_STATE_DIR / "nsfw.db",
    "loupe-apple-enrichment": LOUPE_STATE_DIR / "apple-enrichment.db",
    "loupe-embeddings-siglip2": Path.home() / "loupe/stage5/embeddings_siglip2.db",
    # Added 2026-08-09. These five are in the NAS ledger snapshot but were absent
    # here, so their only off-host copy was the ledger tarball -- which lives on the
    # NAS. Losing the NAS took vault marks and edits with it. vault.db and edits.db
    # in particular are direct human decisions and are not regenerable by re-running
    # anything. tests/test_backup_coverage.py now fails if a ledger database is ever
    # missing from this list again.
    "loupe-vault-marks": LOUPE_STATE_DIR / "vault.db",
    "loupe-edits": LOUPE_STATE_DIR / "edits.db",
    "loupe-renders": LOUPE_STATE_DIR / "renders.db",
    "loupe-pairs": LOUPE_STATE_DIR / "pairs.db",
    "loupe-summaries": LOUPE_STATE_DIR / "summaries.db",
    "loupe-aesthetic-preferences": Path.home() / "loupe/aesthetic/preferences.db",
    # Loupe video analysis. Lives outside ~/loupe, which is why it was missed
    # when this set was first built on 2026-08-07.
    "loupe-video-signals": Path.home() / "loupe-ml/video/video_signals.db",
    "loupe-video-faces-progress": Path.home() / "loupe-ml/video/video-faces/progress.db",
    # Not Loupe: Homestead's knowledge base. Homestead runs on alpha while this
    # database lives here, so it belongs to whichever host holds the file.
    "homestead-kb": Path.home() / "homestead-kb/homestead_kb.db",
}


# Containerised PostgreSQL. Gallery holds photo metadata, faces and CLIP
# embeddings in pgvector/pg14; the SQLite backup API cannot reach it, so it is
# dumped with pg_dump inside the container. Credentials are read from the
# container's own environment and never appear on our argv or in a log.
PG_SOURCES = {
    "gallery-postgres": {
        "container": "gallery_postgres",
        "user": "postgres",
        "database": "gallery",
    },
}


def pg_dump(spec: dict, destination: Path) -> None:
    """Custom-format dump, verified by pg_restore --list before it is accepted."""
    cmd = [
        "docker", "exec", spec["container"],
        "pg_dump", "-U", spec["user"], "-d", spec["database"],
        "--format=custom", "--no-owner", "--no-acl",
    ]
    with destination.open("wb") as out:
        proc = subprocess.run(cmd, stdout=out, stderr=subprocess.PIPE, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(
            "pg_dump failed for %s: %s" % (spec["container"], proc.stderr.decode()[:400])
        )
    if destination.stat().st_size == 0:
        raise RuntimeError("pg_dump produced an empty archive for %s" % spec["container"])
    # Validate the archive. pg_restore is usually absent on the host -- the client
    # tools live in the container -- and a missing executable raises rather than
    # returning non-zero, so catch that explicitly before falling back.
    check = None
    try:
        check = subprocess.run(
            ["pg_restore", "--list", str(destination)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        check = None
    if check is None or check.returncode != 0:
        check = subprocess.run(
            ["docker", "exec", "-i", spec["container"], "pg_restore", "--list"],
            input=destination.read_bytes(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    if check.returncode != 0:
        raise RuntimeError(
            "archive did not validate for %s: %s"
            % (spec["container"], check.stderr.decode()[:400])
        )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_backup(source: Path, destination: Path) -> None:
    source_uri = f"file:{source}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)
            result = destination_db.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"quick_check failed for {source}: {result!r}")


def prune(root: Path, keep_days: int, now_epoch: float) -> list[str]:
    removed: list[str] = []
    threshold = now_epoch - keep_days * 86400
    for candidate in sorted(root.glob("20????????T??????Z")):
        if candidate.is_dir() and candidate.stat().st_mtime < threshold:
            shutil.rmtree(candidate)
            removed.append(candidate.name)
    return removed


def run(root: Path, keep_days: int) -> dict:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)

    final_dir = root / stamp
    if final_dir.exists():
        raise FileExistsError(final_dir)

    with tempfile.TemporaryDirectory(prefix=f".{stamp}.", dir=root) as temp_name:
        temp_dir = Path(temp_name)
        records: list[dict] = []
        missing: list[str] = []
        for label, source in SOURCES.items():
            if not source.is_file():
                missing.append(str(source))
                continue
            destination = temp_dir / f"{label}.sqlite3"
            sqlite_backup(source, destination)
            os.chmod(destination, 0o600)
            records.append(
                {
                    "label": label,
                    "source": str(source),
                    "backup": destination.name,
                    "bytes": destination.stat().st_size,
                    "sha256": sha256(destination),
                    "quick_check": "ok",
                }
            )

        for label, spec in PG_SOURCES.items():
            destination = temp_dir / f"{label}.dump"
            try:
                pg_dump(spec, destination)
            except Exception as exc:                       # container down = optional source
                missing.append("%s (%s)" % (spec["container"], exc))
                if destination.exists():
                    destination.unlink()
                continue
            os.chmod(destination, 0o600)
            records.append(
                {
                    "label": label,
                    "source": "docker:%s/%s" % (spec["container"], spec["database"]),
                    "backup": destination.name,
                    "bytes": destination.stat().st_size,
                    "sha256": sha256(destination),
                    "quick_check": "pg_restore --list ok",
                }
            )

        if not records:
            raise RuntimeError("no configured SQLite sources were present")

        manifest = {
            "schema": "fleet-loupe-data-backup-v1",
            "created_at": now.isoformat(),
            "host": os.uname().nodename,
            "retention_days": keep_days,
            "records": records,
            "missing_optional_sources": missing,
        }
        manifest_path = temp_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        os.chmod(manifest_path, 0o600)
        temp_dir.rename(final_dir)

    latest = root / "latest"
    temporary_link = root / f".latest.{stamp}"
    temporary_link.symlink_to(final_dir.name)
    temporary_link.replace(latest)
    removed = prune(root, keep_days, now.timestamp())
    return {"snapshot": str(final_dir), "records": records, "removed": removed}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.home() / "FleetDatabaseBackups/charlie-snapshots",
    )
    parser.add_argument("--keep-days", type=int, default=14)
    args = parser.parse_args()
    if args.keep_days < 2:
        parser.error("--keep-days must be at least 2")
    result = run(args.root.expanduser(), args.keep_days)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
