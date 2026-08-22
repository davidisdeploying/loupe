#!/usr/bin/env python3
"""Emit culling/production_staged.csv from match results + copy/verify state."""
import json, csv, os

res = json.load(open("/home/david/loupe-pipeline/culling/_match_results.json"))
state = json.load(open("/home/david/loupe-pipeline/culling/_copy_state.json"))
OUT = "/home/david/loupe-pipeline/culling/production_staged.csv"

rows = []
nver = 0
for r in res:
    st = state.get(r["uuid"], {})
    verified = bool(st.get("verified"))
    nver += verified
    rows.append({
        "uuid": r["uuid"],
        "original_filename": r["original_filename"],
        "originals_path": r["originals_path"],
        "production_path": st.get("dest", os.path.join(
            "/mnt/nas2/photos/production", str(r["year"]),
            os.path.basename(r["originals_path"]))),
        "file_size_bytes": r["db_size"],
        "sha256": r["sha256"],
        "verified": "y" if verified else "n",
    })

with open(OUT, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["uuid", "original_filename", "originals_path",
        "production_path", "file_size_bytes", "sha256", "verified"])
    w.writeheader()
    w.writerows(rows)

print(f"wrote {OUT}: {len(rows)} rows, {nver} verified=y, {len(rows)-nver} verified=n")
