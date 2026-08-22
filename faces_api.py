"""faces_api.py — Faces, Phases 1 & 2.

Phase 1 (read-only): People view + face-similarity "find more faces" suggestions.
Phase 2 (writes): confirm / reject / snowball loop.

WHAT THIS WRITES: only the `assignments`, `rejections` and (via person_create)
`persons` tables in faces.db, plus the `claimed_person_id` / `dismissed` columns
of the clusters.db sidecar. It NEVER touches the `faces` / `processed` rows or
the embedding BLOBs — face detection is immutable. (It does CREATE the additive
`rejections` table once.)

Stage 1a (people candidates): clusters.db is the REGENERABLE scaffold written by
people_clusters.py (top-15 kNN + components at cosine >= 0.65, stable ids).
candidates() serves ranked UNNAMED clusters; person_create() turns one into a
persons(source='local') row + bulk assignments(source='cluster', confirmed=0),
which the existing confirm/suggestions snowball then refines.

Embeddings (~55.9k x 512 float32 ~= 114 MB) load LAZILY on the first People
request and are cached for the process — never at import time, so a heavy
decode/thumb job at startup isn't taxed by the load. They are immutable, so they
stay cached forever.

ANCHORS (a person's "known faces" — similarity seeds) split into two parts:
  * IMMUTABLE Apple seeds: faces in assets Apple tagged with EXACTLY ONE person
    == this name AND with EXACTLY ONE detected face (one face, one name).
  * LIVE assignments: every faces.assignments row for the person, re-read fresh
    each call. Confirming a suggestion adds a row here, so the anchor set GROWS
    (the snowball) and re-running suggestions pulls in more of the same person.

REJECTIONS: `assignments.face_id` is the PRIMARY KEY, so that table can hold only
one person per face — it cannot represent "face X is NOT person A" while X may
still be person B. So rejections live in their own additive table keyed on
(person_id, face_id); a reject excludes that face from THAT person's future
suggestions only, and deletes nothing.

WRITE PATTERN mirrors decisions.db in server.py: one long-lived
check_same_thread=False connection in WAL mode, guarded by a threading.Lock,
upsert + commit. Mutable tables (assignments/rejections) are small, so they're
re-read fresh on every request rather than mirrored in memory.
"""
import io
import json
import os
import sqlite3
import threading
import time

from loupe_common import ro as _ro   # shared read-only DB helper (paths come via init/_CFG)

_CFG = {
    "FACES_DB": None,
    "CLUSTERS_DB": None,   # sidecar scaffold from people_clusters.py (sibling of FACES_DB)
    "METADATA_DB": None,
    "ENRICH_DB": None,
    "THUMBS": None,
    "CACHE_DIR": None,
    "thumb_path_fn": None,     # (asset_id) -> ensured thumb path or None
    "preview_path_fn": None,   # (asset_id) -> ensured hi-res preview path or None
}

# immutable in-memory cache (lazy, process-lifetime) -------------------------
_lock = threading.Lock()
_EMB = None        # (N, 512) float32, L2-normalized
_FID = None        # (N,) int64  face_id
_AID = None        # (N,) int64  asset_id
_DET = None        # (N,) float32 det_score
_ROW_BY_FID = None # {face_id: row index}
_ASSET_ROWS = None # {asset_id: [row index, ...]}
_PERSONS = None    # [{person_id, name, source, is_protected}, ...]
_APPLE_ANCHORS = None  # {person_id: [row index, ...]} immutable Apple seeds (det desc)
_APPLE_ASSETS = None   # {person_id: apple asset count}

# write connection (faces.db, read-write) -----------------------------------
_wlock = threading.Lock()
_wconn = None

ANCHOR_CAP = 300   # cap anchors used in the localworker


def init(FACES_DB, METADATA_DB, ENRICH_DB, THUMBS, CACHE_DIR,
         thumb_path_fn, preview_path_fn):
    global _wconn
    _CFG.update(FACES_DB=FACES_DB, METADATA_DB=METADATA_DB, ENRICH_DB=ENRICH_DB,
                THUMBS=THUMBS, CACHE_DIR=CACHE_DIR,
                thumb_path_fn=thumb_path_fn, preview_path_fn=preview_path_fn,
                CLUSTERS_DB=os.path.join(os.path.dirname(FACES_DB), "clusters.db"))
    os.makedirs(os.path.join(CACHE_DIR, "facecrop"), exist_ok=True)
    # one long-lived write connection, mirroring decisions.db's pattern
    _wconn = sqlite3.connect(FACES_DB, check_same_thread=False)
    _wconn.execute("PRAGMA journal_mode=WAL")
    _wconn.execute("PRAGMA busy_timeout=4000")
    # additive rejection store: (person_id, face_id) the user said "not this person"
    _wconn.execute("""CREATE TABLE IF NOT EXISTS rejections(
        person_id INTEGER NOT NULL,
        face_id   INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY(person_id, face_id))""")
    _wconn.commit()




# ---------------------------------------------------------------------------
# lazy load: embeddings + immutable Apple anchor seeds
# ---------------------------------------------------------------------------
def _ensure_loaded():
    global _EMB, _FID, _AID, _DET, _ROW_BY_FID, _ASSET_ROWS
    global _PERSONS, _APPLE_ANCHORS, _APPLE_ASSETS
    if _EMB is not None:
        return
    with _lock:
        if _EMB is not None:
            return
        import numpy as np
        from collections import defaultdict

        fc = _ro(_CFG["FACES_DB"])
        rows = fc.execute(
            "SELECT face_id, asset_id, det_score, embedding FROM faces "
            "ORDER BY face_id").fetchall()
        n = len(rows)
        emb = np.empty((n, 512), dtype=np.float32)
        fid = np.empty(n, dtype=np.int64)
        aid = np.empty(n, dtype=np.int64)
        det = np.empty(n, dtype=np.float32)
        for i, r in enumerate(rows):
            emb[i] = np.frombuffer(r["embedding"], dtype=np.float32)
            fid[i] = r["face_id"]
            aid[i] = r["asset_id"]
            det[i] = r["det_score"] if r["det_score"] is not None else 0.0
        del rows
        # L2-normalize so a dot product == cosine (embeddings are raw, ~22 norm).
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        emb /= norms

        row_by_fid = {int(f): i for i, f in enumerate(fid)}
        asset_rows = defaultdict(list)
        for i, a in enumerate(aid):
            asset_rows[int(a)].append(i)

        persons = [dict(r) for r in fc.execute(
            "SELECT person_id, name, source, is_protected FROM persons "
            "ORDER BY person_id")]
        name2pid = {p["name"]: p["person_id"] for p in persons}
        fc.close()

        # immutable Apple seeds: single-name + single-detected-face assets
        enr = _ro(_CFG["ENRICH_DB"])
        apple_names = defaultdict(set)   # asset_id -> {name}
        for r in enr.execute("SELECT asset_id, person FROM persons "
                             "WHERE person IS NOT NULL AND person <> ''"):
            apple_names[r["asset_id"]].add(r["person"])
        enr.close()

        seed_sets = {p["person_id"]: set() for p in persons}
        apple_assets = {p["person_id"]: 0 for p in persons}
        for asset, names in apple_names.items():
            for nm in names:
                pid = name2pid.get(nm)
                if pid is not None:
                    apple_assets[pid] += 1
            if len(names) != 1:
                continue
            pid = name2pid.get(next(iter(names)))
            if pid is None:
                continue
            ar = asset_rows.get(asset)
            if ar and len(ar) == 1:
                seed_sets[pid].add(ar[0])
        apple_anchors = {pid: sorted(s, key=lambda i: -float(det[i]))
                         for pid, s in seed_sets.items()}

        _EMB, _FID, _AID, _DET = emb, fid, aid, det
        _ROW_BY_FID, _ASSET_ROWS = row_by_fid, dict(asset_rows)
        _PERSONS, _APPLE_ANCHORS, _APPLE_ASSETS = persons, apple_anchors, apple_assets


# ---------------------------------------------------------------------------
# live (mutable) state — assignments + rejections, re-read fresh per request
# ---------------------------------------------------------------------------
def _live_state():
    """Read assignments + rejections now. Returns row-index sets keyed by person.
       (Row index == position in the embedding arrays, via _ROW_BY_FID.)"""
    assign_by_person = {}    # pid -> set(row)  ALL assignments (any confirmed)
    confirmed_by_person = {} # pid -> set(row)  confirmed=1 only
    confirmed_global = set() # set(row)         confirmed=1 for ANY person
    rejected_by_person = {}  # pid -> set(row)
    with _wlock:
        arows = _wconn.execute(
            "SELECT face_id, person_id, confirmed FROM assignments").fetchall()
        rrows = _wconn.execute(
            "SELECT person_id, face_id FROM rejections").fetchall()
    for face_id, pid, confirmed in arows:
        ri = _ROW_BY_FID.get(face_id)
        if ri is None:
            continue
        assign_by_person.setdefault(pid, set()).add(ri)
        if confirmed:
            confirmed_by_person.setdefault(pid, set()).add(ri)
            confirmed_global.add(ri)
    for pid, face_id in rrows:
        ri = _ROW_BY_FID.get(face_id)
        if ri is not None:
            rejected_by_person.setdefault(pid, set()).add(ri)
    return {"assign": assign_by_person, "confirmed": confirmed_by_person,
            "confirmed_global": confirmed_global, "rejected": rejected_by_person}


def _person_centroids(live):
    """Unit centroid per named person, from that person's anchor faces.

    _EMB is L2-normalised, so the normalised mean of a set is a fair centroid and a dot
    product against it is cosine similarity. Computed once per request over ~20 people,
    which is cheap enough not to need caching and avoids a cache that could go stale
    against a confirm made seconds earlier.
    """
    import numpy as np
    out = {}
    for p in _PERSONS:
        pid = p["person_id"]
        rows = [_ROW_BY_FID[f] for f in _anchors_for(pid, live) if f in _ROW_BY_FID]
        if not rows:
            continue
        v = _EMB[rows].mean(axis=0)
        n = float(np.linalg.norm(v))
        if n > 0:
            out[pid] = (v / n, p["name"])
    return out


def _suggest_person(cdb, cid, aset, cents):
    """Nearest named person for a cluster, or None.

    9.3's triage mode wants "K confirm-merge into suggested person", which needs a
    suggestion per CLUSTER -- the existing suggestion machinery is per-person ("find more
    faces like this one") and cannot answer it. This compares the cluster's centroid to
    each person's, using embeddings already in memory.

    Bounded to the 60 faces closest to the cluster centroid: the centroid of a
    representative sample is the centroid, and an unbounded IN on a 149-face cluster buys
    nothing. Never raises -- a missing suggestion degrades triage to manual naming, an
    exception would take the whole People page down with it.
    """
    import numpy as np
    try:
        fids = [f[0] for f in cdb.execute(
            "SELECT face_id FROM cluster_faces WHERE cluster_id=?"
            " ORDER BY cos_to_centroid DESC LIMIT 60", (cid,))
            if f[0] not in aset]
        rows = [_ROW_BY_FID[f] for f in fids if f in _ROW_BY_FID]
        if not rows or not cents:
            return None
        v = _EMB[rows].mean(axis=0)
        n = float(np.linalg.norm(v))
        if n <= 0:
            return None
        v = v / n
        best_pid, best_name, best_score = None, None, -1.0
        for pid, (c, name) in cents.items():
            sc = float(v @ c)
            if sc > best_score:
                best_pid, best_name, best_score = pid, name, sc
        if best_pid is None:
            return None
        # The nearest named person is NOT the same as a match. Most unnamed clusters
        # are genuinely nobody in the person list, and this function will still return
        # whoever is closest -- measured on this library the field runs 0.047 to 0.546,
        # i.e. mostly "not them". Presenting that as a suggestion would be a confident
        # guess wearing the clothes of an answer, so the score travels with it and
        # `confident` gates whether a UI may pre-fill anything.
        #
        # The bar is PROVISIONAL and uncalibrated: centroid-to-centroid similarity runs
        # lower than the face-to-face 0.88 used elsewhere, because averaging pulls
        # magnitude toward the mean. Calibrating it needs known-correct cluster/person
        # pairs, which is a labelling job, not a code change.
        return {"person_id": best_pid, "name": best_name,
                "score": round(best_score, 4),
                "confident": bool(best_score >= 0.45)}
    except Exception:
        return None


def _anchors_for(pid, live):
    """Full anchor set (immutable Apple seeds + every live assignment row)."""
    s = set(_APPLE_ANCHORS.get(pid, []))
    s |= live["assign"].get(pid, set())
    return s


def _sampled_anchor_idx(pid, live):
    """Anchor rows for the localworker, capped at ANCHOR_CAP. Live assignment rows
       (the curated/confirmed ones) are ALWAYS kept; Apple seeds fill the rest
       (evenly sampled) — so a confirm always influences the next suggestions."""
    import numpy as np
    assign = list(live["assign"].get(pid, set()))
    if len(assign) >= ANCHOR_CAP:
        return assign[:ANCHOR_CAP]
    sel = list(assign)
    need = ANCHOR_CAP - len(sel)
    aset = set(assign)
    pool = [r for r in _APPLE_ANCHORS.get(pid, []) if r not in aset]
    if len(pool) > need:
        idx = np.linspace(0, len(pool) - 1, need).astype(int)
        pool = [pool[i] for i in idx]
    return sel + pool


# ---------------------------------------------------------------------------
# read-only models
# ---------------------------------------------------------------------------
def people_list():
    _ensure_loaded()
    live = _live_state()
    out = []
    for p in _PERSONS:
        pid = p["person_id"]
        anc = _anchors_for(pid, live)
        rep = None
        if anc:
            rep = int(_FID[max(anc, key=lambda i: float(_DET[i]))])
        out.append({
            "person_id": pid, "name": p["name"], "source": p["source"],
            "is_protected": bool(p["is_protected"]),
            "known_faces": len(anc),
            "confirmed": len(live["confirmed"].get(pid, set())),
            "apple_assets": _APPLE_ASSETS.get(pid, 0),
            "rep_face_id": rep,
        })
    out.sort(key=lambda r: -r["known_faces"])
    # 9.3 asks People for a progress header, because this is where completeness
    # visualisation pays off most: 9,724 clusters is a triage queue, not a gallery.
    #
    # Two different numbers, deliberately both. ASSIGNED is faces attached to a named
    # person -- the work actually finished. CLUSTERED is faces grouped at all, which is
    # the size of the queue waiting to be named. The audit quotes "49.8% of faces
    # identified", but that figure was the clustered share; identified is far lower, and
    # conflating them would overstate progress by more than double.
    assigned = clustered = None
    try:
        c = _ro(_CFG["FACES_DB"])
        try:
            assigned = c.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
        finally:
            c.close()
    except Exception:
        pass
    try:
        c = _ro(_CFG["CLUSTERS_DB"])
        try:
            clustered = c.execute("SELECT COUNT(*) FROM cluster_faces").fetchone()[0]
        finally:
            c.close()
    except Exception:
        pass
    return {"people": out, "total_faces": int(_FID.shape[0]),
            "identified_faces": assigned, "clustered_faces": clustered}


def person_detail(pid, limit=300):
    """The person's whole gathered set — Apple seeds + confirmed faces — in one
       gallery (the culling payoff). Each face flagged confirmed vs seed."""
    _ensure_loaded()
    p = next((x for x in _PERSONS if x["person_id"] == pid), None)
    if not p:
        return {"error": "no such person"}
    live = _live_state()
    anc = _anchors_for(pid, live)
    conf = live["confirmed"].get(pid, set())
    # confirmed faces first (the user's curated additions — the payoff), then the
    # rest by detection score; otherwise newly-confirmed faces can sort below the
    # display cap and stay invisible.
    ordered = sorted(anc, key=lambda i: (i not in conf, -float(_DET[i])))
    faces = [{"face_id": int(_FID[i]), "asset_id": int(_AID[i]),
              "det_score": round(float(_DET[i]), 3), "confirmed": i in conf}
             for i in ordered[:limit]]
    return {
        "person_id": pid, "name": p["name"], "is_protected": bool(p["is_protected"]),
        "known_faces": len(anc), "confirmed": len(conf), "shown": len(faces),
        "apple_assets": _APPLE_ASSETS.get(pid, 0), "faces": faces,
    }


def suggestions(pid, k=150):
    """Top-k faces NOT already attached to this person and not rejected for them,
       ranked by best cosine to any anchor. Excludes faces confirmed to ANY
       person (spoken for). Display-only until confirmed."""
    _ensure_loaded()
    import numpy as np
    p = next((x for x in _PERSONS if x["person_id"] == pid), None)
    if not p:
        return {"error": "no such person"}
    live = _live_state()
    anc_all = _anchors_for(pid, live)
    if not anc_all:
        return {"person_id": pid, "name": p["name"], "anchors": 0,
                "anchors_used": 0, "suggestions": [],
                "note": "no face suggestions yet"}
    anc = _sampled_anchor_idx(pid, live)
    A = _EMB[anc]
    best = (A @ _EMB.T).max(axis=0)
    exclude = anc_all | live["confirmed_global"] | live["rejected"].get(pid, set())
    if exclude:
        best[np.fromiter(exclude, dtype=np.int64, count=len(exclude))] = -2.0
    k = max(1, min(int(k), 500))
    top = np.argpartition(-best, k)[:k]
    top = top[np.argsort(-best[top])]
    sug = [{"face_id": int(_FID[i]), "asset_id": int(_AID[i]),
            "score": round(float(best[i]), 4),
            "det_score": round(float(_DET[i]), 3)}
           for i in top if best[i] > -1.0]
    return {"person_id": pid, "name": p["name"],
            "anchors": len(anc_all), "anchors_used": len(anc),
            "suggestions": sug}


def candidates(strict=False, limit=25):
    """Ranked UNNAMED person-candidate clusters from the clusters.db scaffold,
       distinct-days desc (recurrence metric of record — raw face counts are
       burst-inflated). Bars calibrated by the 2026-07-05 recon: default
       >=10 assets & >=5 days (~88), strict >=20 & >=10 (~33). Excludes
       clusters already claimed via person_create, clusters dismissed via
       candidate_dismiss, and faces already assigned to a named person."""
    _ensure_loaded()
    cpath = _CFG["CLUSTERS_DB"]
    if not cpath or not os.path.exists(cpath):
        return {"candidates": [], "total": 0,
                "note": "no cluster store — run people_clusters.py"}
    min_a, min_d = (20, 10) if strict else (10, 5)
    limit = max(1, min(int(limit), 100))
    with _wlock:
        assigned = {r[0] for r in
                    _wconn.execute("SELECT face_id FROM assignments")}
    cdb = _ro(cpath)
    # `dismissed` is a later additive column — tolerate a pre-dismiss sidecar
    have_dis = any(r[1] == "dismissed" for r in
                   cdb.execute("PRAGMA table_info(clusters)"))
    rows = cdb.execute(
        "SELECT cluster_id, size, n_assets, n_days, first_day, last_day,"
        " rep_face_ids FROM clusters"
        " WHERE claimed_person_id IS NULL"
        + (" AND COALESCE(dismissed,0)=0" if have_dis else "")
        + " AND n_assets>=? AND n_days>=?"
        " ORDER BY n_days DESC, n_assets DESC", (min_a, min_d)).fetchall()
    # which already-assigned faces fall in which cluster (bounded IN chunks —
    # NEVER bind a container; build (?,...) and bind a sorted list)
    assigned_in = {}
    aids = sorted(assigned)
    for i in range(0, len(aids), 500):
        chunk = aids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        for cid, fid in cdb.execute(
                "SELECT cluster_id, face_id FROM cluster_faces"
                f" WHERE face_id IN ({ph})", chunk):
            assigned_in.setdefault(cid, set()).add(fid)
    # candidates() has no live state of its own; the centroids need the same
    # anchor view people_list() uses, so derive it here rather than passing it in.
    _CENTS = _person_centroids(_live_state())
    out, total = [], 0
    for r in rows:
        cid = r["cluster_id"]
        aset = assigned_in.get(cid, set())
        unassigned = r["size"] - len(aset)
        if unassigned <= 0:            # fully spoken for — not a candidate
            continue
        total += 1
        if len(out) >= limit:
            continue
        reps = [f for f in json.loads(r["rep_face_ids"]) if f not in aset]
        if not reps:                   # all stored reps assigned — refetch
            reps = [f[0] for f in cdb.execute(
                "SELECT face_id FROM cluster_faces WHERE cluster_id=?"
                " ORDER BY cos_to_centroid DESC LIMIT 50", (cid,))
                if f[0] not in aset][:12]
        out.append({
            "cluster_id": cid, "size": r["size"], "unassigned": unassigned,
            "n_assets": r["n_assets"], "n_days": r["n_days"],
            "first_day": r["first_day"], "last_day": r["last_day"],
            "rep_face_ids": reps,
            "suggest": _suggest_person(cdb, cid, aset, _CENTS),
        })
    cdb.close()
    return {"strict": bool(strict), "min_assets": min_a, "min_days": min_d,
            "total": total, "shown": len(out), "candidates": out}


# ---------------------------------------------------------------------------
# write path — assignments + rejections ONLY (never faces/embeddings)
# ---------------------------------------------------------------------------
def _valid_person(pid):
    return any(x["person_id"] == pid for x in _PERSONS)


def confirm(pid, faces):
    """faces: [{face_id, score}, ...]. Write confirmed propagated assignments."""
    _ensure_loaded()
    if not _valid_person(pid):
        return {"error": "no such person"}
    now = int(time.time())
    written = 0
    with _wlock:
        cur = _wconn.cursor()
        for f in faces:
            try:
                fid = int(f["face_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if fid not in _ROW_BY_FID:        # must be a real detected face
                continue
            conf = f.get("score")
            conf = float(conf) if conf is not None else None
            cur.execute(
                "INSERT INTO assignments"
                "(face_id, person_id, source, confidence, confirmed, updated_at) "
                "VALUES(?,?, 'propagated', ?, 1, ?) "
                "ON CONFLICT(face_id) DO UPDATE SET person_id=excluded.person_id,"
                "source=excluded.source, confidence=excluded.confidence,"
                "confirmed=1, updated_at=excluded.updated_at",
                (fid, pid, conf, now))
            written += 1
        _wconn.commit()
    return {"confirmed": written}


def reject(pid, face_ids):
    """Record (pid, face_id) rejections — excluded from this person's future
       suggestions. Deletes nothing."""
    _ensure_loaded()
    if not _valid_person(pid):
        return {"error": "no such person"}
    now = int(time.time())
    written = 0
    with _wlock:
        cur = _wconn.cursor()
        for fid in face_ids:
            try:
                fid = int(fid)
            except (TypeError, ValueError):
                continue
            if fid not in _ROW_BY_FID:
                continue
            cur.execute(
                "INSERT OR IGNORE INTO rejections(person_id, face_id, updated_at) "
                "VALUES(?,?,?)", (pid, fid, now))
            written += 1
        _wconn.commit()
    return {"rejected": written}


def person_create(name, cluster_id):
    """Name an unnamed cluster: INSERT persons(source='local') + bulk
       assignments(source='cluster', confidence=cos-to-centroid, confirmed=0)
       for the cluster's faces — the existing confirm/suggestions snowball then
       refines. INSERT OR IGNORE (face_id is the assignments PK) skips faces
       already assigned to any person; a recluster can never un-name because
       naming lives here, not in clusters.db. Never touches faces/embeddings."""
    _ensure_loaded()
    name = (name or "").strip()
    if not name:
        return {"error": "empty name"}
    try:
        cluster_id = int(cluster_id)
    except (TypeError, ValueError):
        return {"error": "bad cluster_id"}
    cpath = _CFG["CLUSTERS_DB"]
    if not cpath or not os.path.exists(cpath):
        return {"error": "no cluster store — run people_clusters.py"}
    cdb = _ro(cpath)
    row = cdb.execute("SELECT claimed_person_id FROM clusters WHERE cluster_id=?",
                      (cluster_id,)).fetchone()
    if not row:
        cdb.close()
        return {"error": "no such cluster"}
    if row["claimed_person_id"] is not None:
        cdb.close()
        return {"already_named": True, "person_id": row["claimed_person_id"],
                "cluster_id": cluster_id, "note": "cluster already named — no-op"}
    cfaces = cdb.execute(
        "SELECT face_id, cos_to_centroid FROM cluster_faces WHERE cluster_id=?",
        (cluster_id,)).fetchall()
    cdb.close()
    now = int(time.time())
    with _wlock:
        dup = _wconn.execute(
            "SELECT person_id FROM persons WHERE name=? COLLATE NOCASE",
            (name,)).fetchone()
        if dup:
            return {"error": "duplicate name", "person_id": dup[0]}
        cur = _wconn.cursor()
        try:
            cur.execute("INSERT INTO persons(name, is_protected, source,"
                        " created_at) VALUES(?, 0, 'local', ?)", (name, now))
        except sqlite3.IntegrityError:       # UNIQUE(name) race
            _wconn.rollback()
            return {"error": "duplicate name"}
        pid = cur.lastrowid
        written = 0
        for f in cfaces:
            cur.execute(
                "INSERT OR IGNORE INTO assignments"
                "(face_id, person_id, source, confidence, confirmed, updated_at)"
                " VALUES(?, ?, 'cluster', ?, 0, ?)",
                (f["face_id"], pid, f["cos_to_centroid"], now))
            written += cur.rowcount
        _wconn.commit()
    # mark the cluster claimed (sidecar; rare op, short-lived rw connection)
    cw = sqlite3.connect(cpath)
    cw.execute("PRAGMA busy_timeout=4000")
    cw.execute("UPDATE clusters SET claimed_person_id=? WHERE cluster_id=?",
               (pid, cluster_id))
    cw.commit()
    cw.close()
    # refresh the process-lifetime persons cache so the snowball (suggestions/
    # confirm) sees the new person without a restart
    with _lock:
        _PERSONS.append({"person_id": pid, "name": name, "source": "local",
                         "is_protected": 0})
        _APPLE_ANCHORS[pid] = []
        _APPLE_ASSETS[pid] = 0
    return {"person_id": pid, "name": name, "cluster_id": cluster_id,
            "faces_assigned": written,
            "faces_skipped_already_assigned": len(cfaces) - written}


def person_assign_cluster(person_id, cluster_id):
    """Fold an unnamed cluster into an EXISTING person (the autocomplete
       "Add to [Name]" path) — no new persons row. Same writes as
       person_create: bulk INSERT OR IGNORE assignments(source='cluster',
       confirmed=0) + claim the cluster in the clusters.db sidecar.
       Confidence = cos to THIS person's anchor centroid when anchors exist
       (embeddings are already in memory, L2-normalized), else the cluster's
       own cos_to_centroid. Never touches faces/embeddings."""
    _ensure_loaded()
    try:
        person_id = int(person_id)
    except (TypeError, ValueError):
        return {"error": "bad person_id"}
    try:
        cluster_id = int(cluster_id)
    except (TypeError, ValueError):
        return {"error": "bad cluster_id"}
    p = next((x for x in _PERSONS if x["person_id"] == person_id), None)
    if not p:
        return {"error": "no such person"}
    cpath = _CFG["CLUSTERS_DB"]
    if not cpath or not os.path.exists(cpath):
        return {"error": "no cluster store — run people_clusters.py"}
    cdb = _ro(cpath)
    row = cdb.execute("SELECT claimed_person_id FROM clusters WHERE cluster_id=?",
                      (cluster_id,)).fetchone()
    if not row:
        cdb.close()
        return {"error": "no such cluster"}
    if row["claimed_person_id"] is not None:
        cdb.close()
        return {"error": "cluster already named",
                "person_id": row["claimed_person_id"]}
    cfaces = cdb.execute(
        "SELECT face_id, cos_to_centroid FROM cluster_faces WHERE cluster_id=?",
        (cluster_id,)).fetchall()
    cdb.close()
    # cos to the person's anchor centroid (dot == cosine on the normalized
    # rows); falls back per-face to the cluster's own cos_to_centroid
    conf_by_fid = {}
    anc = list(_anchors_for(person_id, _live_state()))
    if anc:
        import numpy as np
        c = _EMB[anc].mean(axis=0)
        n = float(np.linalg.norm(c))
        if n > 0:
            c = c / n
            for f in cfaces:
                ri = _ROW_BY_FID.get(f["face_id"])
                if ri is not None:
                    conf_by_fid[f["face_id"]] = round(float(_EMB[ri] @ c), 4)
    now = int(time.time())
    written = 0
    with _wlock:
        cur = _wconn.cursor()
        for f in cfaces:
            cur.execute(
                "INSERT OR IGNORE INTO assignments"
                "(face_id, person_id, source, confidence, confirmed, updated_at)"
                " VALUES(?, ?, 'cluster', ?, 0, ?)",
                (f["face_id"], person_id,
                 conf_by_fid.get(f["face_id"], f["cos_to_centroid"]), now))
            written += cur.rowcount
        _wconn.commit()
    # mark the cluster claimed (sidecar; rare op, short-lived rw connection)
    cw = sqlite3.connect(cpath)
    cw.execute("PRAGMA busy_timeout=4000")
    cw.execute("UPDATE clusters SET claimed_person_id=? WHERE cluster_id=?",
               (person_id, cluster_id))
    cw.commit()
    cw.close()
    # no persons-cache append needed — the person already exists, and
    # _live_state() reads assignments fresh on every call
    return {"person_id": person_id, "name": p["name"], "cluster_id": cluster_id,
            "added_to_existing": True, "faces_assigned": written,
            "faces_skipped_already_assigned": len(cfaces) - written}


def candidate_dismiss(cluster_id):
    """'Not a person': flag the cluster dismissed in the clusters.db SIDECAR so
       candidates() skips it, exactly like a claimed cluster. faces.db is never
       touched — no photo, face, or assignment is affected — and the flag rides
       people_clusters.py's stable-id carry-forward across re-runs. Reversal is
       clearing the column by hand (no un-dismiss UI in the MVP)."""
    try:
        cluster_id = int(cluster_id)
    except (TypeError, ValueError):
        return {"error": "bad cluster_id"}
    cpath = _CFG["CLUSTERS_DB"]
    if not cpath or not os.path.exists(cpath):
        return {"error": "no cluster store — run people_clusters.py"}
    # rare op — short-lived rw connection, same pattern as person_create's claim
    cw = sqlite3.connect(cpath)
    try:
        cw.execute("PRAGMA busy_timeout=4000")
        cols = {r[1] for r in cw.execute("PRAGMA table_info(clusters)")}
        if "dismissed" not in cols:    # pre-dismiss sidecar: additive migrate
            cw.execute("ALTER TABLE clusters ADD COLUMN dismissed INTEGER")
        row = cw.execute("SELECT claimed_person_id FROM clusters"
                         " WHERE cluster_id=?", (cluster_id,)).fetchone()
        if not row:
            return {"error": "no such cluster"}
        if row[0] is not None:
            return {"error": "cluster already named"}
        cw.execute("UPDATE clusters SET dismissed=1 WHERE cluster_id=?",
                   (cluster_id,))
        cw.commit()
    finally:
        cw.close()
    return {"dismissed": True, "cluster_id": cluster_id}


# ---------------------------------------------------------------------------
# face crop — derived from the existing thumb cache (preview only for tiny
# group faces). Cached per face_id to disk. Read-only.
# ---------------------------------------------------------------------------
def _crop_cache(fid):
    return os.path.join(_CFG["CACHE_DIR"], "facecrop", f"{fid}.jpg")


def face_crop(fid):
    cp = _crop_cache(fid)
    if os.path.exists(cp):
        try:
            with open(cp, "rb") as f:
                return f.read()
        except OSError:
            pass
    fc = _ro(_CFG["FACES_DB"])
    row = fc.execute("SELECT asset_id, bbox_json FROM faces WHERE face_id=?",
                     (fid,)).fetchone()
    fc.close()
    if not row or not row["bbox_json"]:
        return None
    try:
        b = json.loads(row["bbox_json"])
        iw, ih = float(b["img_w"]), float(b["img_h"])
    except (ValueError, KeyError):
        return None
    aid = row["asset_id"]

    from PIL import Image
    src = _CFG["thumb_path_fn"](aid)
    if not src or not os.path.exists(src):
        return None
    im = Image.open(src)
    tw, th = im.size
    fw = (b["x2"] - b["x1"]) * (tw / iw)
    if max(fw, (b["y2"] - b["y1"]) * (th / ih)) < 64:
        pv = _CFG["preview_path_fn"](aid)
        if pv and os.path.exists(pv):
            im = Image.open(pv)
            tw, th = im.size
    im = im.convert("RGB")
    sx, sy = tw / iw, th / ih
    x1, y1 = b["x1"] * sx, b["y1"] * sy
    x2, y2 = b["x2"] * sx, b["y2"] * sy
    mw, mh = (x2 - x1) * 0.35, (y2 - y1) * 0.35
    box = (max(0, int(x1 - mw)), max(0, int(y1 - mh)),
           min(tw, int(x2 + mw)), min(th, int(y2 + mh)))
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    crop = im.crop(box)
    long_side = max(crop.size)
    if long_side != 200:
        s = 200.0 / long_side
        crop = crop.resize((max(1, int(crop.size[0] * s)),
                            max(1, int(crop.size[1] * s))), Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=82)
    data = buf.getvalue()
    try:
        tmp = cp + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, cp)
    except OSError:
        pass
    return data
