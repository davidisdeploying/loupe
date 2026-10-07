#!/usr/bin/env python3
"""AI-polished period summaries for loupe — three layers, cached forever.

LAYER 1 (deterministic, local, instant): facts from metadata.db.
LAYER 2a (venues, cached per cluster, library-wide): Google Places, one call per cluster.
LAYER 2b (prose, cached per period): Anthropic Haiku, tight prompt.

Read-only on metadata.db. Keys from ENV ONLY (never hardcoded). Caches to a SEPARATE db
(summaries.db). Any missing key / failed call degrades to Layer-1 — never a broken block.
"""
import json
import os
import sqlite3
import threading
import time

from loupe_common import APP_DATA, METADATA_DB as META, EXCLUDE_SQL, VIDEO_EXT, ro
CACHE_DB = os.path.join(APP_DATA, "summaries.db")
MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
GRID = 75.0 / 111320.0                # ~75 m, in degrees of latitude (cluster grid)
MIN_CLUSTER_FRAMES = 3                # ignore clusters smaller than this (GPS-glitch singletons)
MERGE_KM = 2.0                        # grid cells within this of each other are one "place"
                                      # (so a location isn't split into several fragments)
PLACES_LOG = os.path.join(APP_DATA, "summaries.log")

# --- venue-honesty constants -------------------------------------------------
# Keep real places (the singleton filter above is the only frame-count discount; the old
# far-outlier discount stays gone). But don't over-claim a specific venue when the person
# was likely just home. Two independent guards, both applied at summary assembly:
VENUE_GATE_M = 200.0                  # DISTANCE GATE (primary): name a Place only if it sits
                                      # within this of the cluster centroid. A venue you were
                                      # actually at coincides with the photos (one event
                                      # center resolved 25 m off its 126-frame cluster); a
                                      # nearest-business-to-a-cluster sits farther (a ranch's
                                      # gate-house 391 m away) -> fall back to the area name.
VENUE_SEARCH_M = 400.0                # search a touch wider than the gate so the gate can
                                      # actually reject a too-far nearest business; at radius
                                      # == gate the gate would never fire.
HOME_METRO_KM = 8.0                   # HOME SUPPRESSION (backstop): an area counts as a "home
                                      # suburb" only if its centroid is within this of the home
                                      # centroid AND it recurs in daily life. TIGHT on purpose:
                                      # home is the couple of adjacent suburbs you live across
                                      # (the two you straddle, ~1 and ~5 km out); the next
                                      # suburb over (~15 km) is well clear and keeps its real
                                      # venues. NOT a metro-wide radius.
HOME_SUBURB_MIN_DAYS = 3             # ...and appears on at least this many distinct days, so a
                                      # one-off pass-through or stray geocode isn't called home.
HOME_VENUE_RADIUS_KM = 2.5           # geometric backstop: a cluster this close to the home
                                      # centroid is home no matter how it geocodes. Kept tight;
                                      # a suburb ~15 km out survives.


def anthropic_key():
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def places_key():
    return (os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
            or os.environ.get("GOOGLE_MAPS_API_KEY", "").strip())


def llm_model():
    return os.environ.get("LOUPE_LLM_MODEL", "claude-haiku-4-5").strip()


# --- caches -----------------------------------------------------------------
_db = sqlite3.connect(CACHE_DB, check_same_thread=False)
_db.execute("PRAGMA journal_mode=WAL")
_db.execute("""CREATE TABLE IF NOT EXISTS summaries(
    period_key TEXT PRIMARY KEY, facts_json TEXT, venues_json TEXT,
    prose TEXT, generated_at INTEGER)""")
_db.execute("""CREATE TABLE IF NOT EXISTS clusters(
    cluster_key TEXT PRIMARY KEY, lat REAL, lon REAL, venue TEXT, generated_at INTEGER)""")
# lat/lon hold the CLUSTER centroid; venue_lat/venue_lon hold the resolved Place's own
# coordinate so the distance gate is checkable from cache without re-calling Places.
_cols = {r[1] for r in _db.execute("PRAGMA table_info(clusters)").fetchall()}
if "venue_lat" not in _cols:
    _db.execute("ALTER TABLE clusters ADD COLUMN venue_lat REAL")
if "venue_lon" not in _cols:
    _db.execute("ALTER TABLE clusters ADD COLUMN venue_lon REAL")
# one-time: drop a pre-centroid __home__ row (old code stored lat/lon=0) so the home model
# (centroid + home-suburb set) recomputes on next access.
_db.execute("DELETE FROM clusters WHERE cluster_key='__home__' AND (lat=0 OR lat IS NULL)")
_db.commit()
_dlock = threading.Lock()

_gen_sem = threading.BoundedSemaphore(2)      # cap whole-upgrade (Places+LLM) concurrency
_places_sem = threading.BoundedSemaphore(2)   # cap Places concurrency / rate
_llm_sem = threading.BoundedSemaphore(2)      # cap LLM concurrency
_rows_cache = {}                              # (y,m) -> rows (read-only metadata, cached)
_home = None


def _ro():
    return ro(META)   # loupe_common.ro; keeps the no-arg call sites unchanged


def _fetch_rows(y, m):
    key = (y, m)
    if key in _rows_cache:
        return _rows_cache[key]
    conn = _ro()
    rows = conn.execute(
        f"""SELECT capture_timestamp AS ts, extension AS ext, gps_lat, gps_lon,
                   camera_make, camera_model, duration_seconds AS dur,
                   is_live_photo_still AS lps
            FROM assets WHERE year=? AND month=? AND {EXCLUDE_SQL}""", (y, m)).fetchall()
    conn.close()
    out = [dict(r) for r in rows]
    _rows_cache[key] = out
    return out


def _haversine(la1, lo1, la2, lo2):
    import math
    R, p = 6371.0, math.radians
    return 2 * R * math.asin(math.sqrt(
        math.sin((p(la2) - p(la1)) / 2) ** 2
        + math.cos(p(la1)) * math.cos(p(la2)) * math.sin((p(lo2) - p(lo1)) / 2) ** 2))


def _plog(msg):
    """Diagnostic log for the Places provider — status/error text only, NEVER the key."""
    try:
        with open(PLACES_LOG, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# --- offline reverse geocode (city/area) ------------------------------------
def _area(rec):
    name, a1 = rec.get("name"), rec.get("admin1")
    return f"{name}, {a1}" if name else None


def geocode_batch(coords):
    if not coords:
        return []
    try:
        import reverse_geocoder as rg
        recs = rg.search([(float(a), float(b)) for a, b in coords], mode=1)
        return [_area(r) for r in recs]
    except Exception:
        return [None] * len(coords)


def _home_model():
    """The home model, computed once from a recent sample and cached forever:
      lat/lon   the home centroid (the ~400 m cell seen on the MOST distinct days -- a trip is
                many frames over few days, home is the opposite, so day-count finds the residence
                even when a trip out-frames it);
      area      the modal city/area name (kept for facts/trips, unchanged behaviour);
      suburbs   the set of adjacent suburbs daily life happens across (near the home centroid and
                recurring) -- these are named by AREA, never by their nearest shop.
    """
    global _home
    if _home is not None:
        return _home
    with _dlock:
        rc = _db.execute("SELECT lat, lon, venue FROM clusters WHERE cluster_key='__home__'").fetchone()
        rs = _db.execute("SELECT venue FROM clusters WHERE cluster_key='__home_suburbs__'").fetchone()
    if rc and rs and rc[0]:
        _home = {"lat": rc[0], "lon": rc[1], "area": rc[2] or "",
                 "suburbs": set(json.loads(rs[0] or "[]"))}
        return _home
    conn = _ro()
    rows = conn.execute(f"SELECT gps_lat, gps_lon, capture_timestamp FROM assets "
                        f"WHERE gps_lat IS NOT NULL AND {EXCLUDE_SQL} "
                        f"ORDER BY id DESC LIMIT 8000").fetchall()
    conn.close()
    areas = geocode_batch([(r[0], r[1]) for r in rows])
    # home centroid: the ~400 m cell present on the most distinct calendar days
    hcell = 400.0 / 111320.0
    cdays, cacc = {}, {}
    for la, lo, ts in rows:
        k = (round(la / hcell), round(lo / hcell))
        cdays.setdefault(k, set())
        if ts is not None:
            cdays[k].add(time.strftime("%Y-%m-%d", time.localtime(ts)))
        a = cacc.setdefault(k, [0.0, 0.0, 0]); a[0] += la; a[1] += lo; a[2] += 1
    if cdays:
        hk = max(cdays, key=lambda k: len(cdays[k])); a = cacc[hk]
        hlat, hlon = a[0] / a[2], a[1] / a[2]
    else:
        hlat = hlon = 0.0
    # modal area (unchanged) + the home-suburb set (near the centroid AND recurring in daily life)
    adays, aacc = {}, {}
    for (la, lo, ts), ar in zip(rows, areas):
        if not ar:
            continue
        adays.setdefault(ar, set())
        if ts is not None:
            adays[ar].add(time.strftime("%Y-%m-%d", time.localtime(ts)))
        p = aacc.setdefault(ar, [0.0, 0.0, 0]); p[0] += la; p[1] += lo; p[2] += 1
    modal = max(aacc, key=lambda ar: aacc[ar][2]) if aacc else ""
    suburbs = {ar for ar, p in aacc.items()
               if _haversine(hlat, hlon, p[0] / p[2], p[1] / p[2]) <= HOME_METRO_KM
               and len(adays[ar]) >= HOME_SUBURB_MIN_DAYS}
    if modal:
        suburbs.add(modal)                       # the modal area is always home
    _home = {"lat": hlat, "lon": hlon, "area": modal, "suburbs": suburbs}
    with _dlock:
        _db.execute("INSERT OR REPLACE INTO clusters(cluster_key,lat,lon,venue,generated_at) "
                    "VALUES('__home__',?,?,?,?)", (hlat, hlon, modal, int(time.time())))
        _db.execute("INSERT OR REPLACE INTO clusters(cluster_key,lat,lon,venue,generated_at) "
                    "VALUES('__home_suburbs__',0,0,?,?)",
                    (json.dumps(sorted(suburbs)), int(time.time())))
        _db.commit()
    return _home


def home_area():
    """The library's modal city/area (back-compat for facts/trips)."""
    return _home_model()["area"]


# --- LAYER 1: deterministic facts -------------------------------------------
_TOD = [("night", 0, 5), ("morning", 6, 11), ("afternoon", 12, 17), ("evening", 18, 23)]


def _dayof(ts, y, m):
    if ts is None:
        return 0
    lt = time.localtime(ts)
    return lt.tm_mday if (lt.tm_year == y and lt.tm_mon == m) else 0


def _label(level, y, m, d, wk):
    if level == "day":
        return f"{MON[m]} {d}, {y}"
    if level == "week":
        if wk == 9:
            return f"{MON[m]} {y} (undated)"
        s = wk * 7 + 1
        return f"{MON[m]} {s}–{s + 6}, {y}"
    return f"{MON[m]} {y}"


def facts_for(level, y, m, d=None, wk=None):
    rows = _fetch_rows(y, m)
    if level == "day":
        sel = [r for r in rows if _dayof(r["ts"], y, m) == d]
    elif level == "week":
        sel = [r for r in rows if (9 if _dayof(r["ts"], y, m) == 0
                                   else min(4, (_dayof(r["ts"], y, m) - 1) // 7)) == wk]
    else:
        sel = rows

    frames = len(sel)
    clips = sum(1 for r in sel if (r["ext"] or "").upper() in VIDEO_EXT)
    live = sum(1 for r in sel if r["lps"])
    durs = [r["dur"] for r in sel if (r["ext"] or "").upper() in VIDEO_EXT and r["dur"]]
    longest = round(max(durs)) if durs else 0

    tod_counts = {n: 0 for n, _, _ in _TOD}
    for r in sel:
        if r["ts"]:
            h = time.localtime(r["ts"]).tm_hour
            for n, a, b in _TOD:
                if a <= h <= b:
                    tod_counts[n] += 1
    tod = max(tod_counts, key=tod_counts.get) if frames else None

    cams = {}
    for r in sel:
        cm = " ".join(x for x in (r["camera_make"], r["camera_model"]) if x).strip()
        if cm:
            cams[cm] = cams.get(cm, 0) + 1
    cameras = [c for c, _ in sorted(cams.items(), key=lambda kv: -kv[1])[:2]]

    busiest, spike = None, False
    if level in ("week", "month"):
        byday = {}
        for r in sel:
            dd = _dayof(r["ts"], y, m)
            if dd:
                byday[dd] = byday.get(dd, 0) + 1
        if byday:
            bd = max(byday, key=byday.get)
            mean = sum(byday.values()) / len(byday)
            busiest = {"day": bd, "frames": byday[bd]}
            spike = byday[bd] >= max(20, 2.5 * mean) and len(byday) > 1

    # location: grid cells -> merge nearby cells into one "place" -> artifact guard
    cells = {}
    for r in sel:
        if r["gps_lat"] is None:
            continue
        la, lo = r["gps_lat"], r["gps_lon"]
        ck = f"{round(la / GRID)}_{round(lo / GRID)}"
        b = cells.setdefault(ck, [0, 0.0, 0.0, []])
        b[0] += 1; b[1] += la; b[2] += lo
        if r["ts"]:
            b[3].append(r["ts"])
    cell_list = sorted(([n, sla / n, slo / n, ts] for n, sla, slo, ts in cells.values()),
                       key=lambda c: -c[0])
    sup = []
    for n, la, lo, ts in cell_list:
        for s in sup:
            if _haversine(s["lat"], s["lon"], la, lo) <= MERGE_KM:
                tot = s["n"] + n
                s["lat"] = (s["lat"] * s["n"] + la * n) / tot
                s["lon"] = (s["lon"] * s["n"] + lo * n) / tot
                s["n"] = tot; s["ts"].extend(ts); break
        else:
            sup.append({"n": n, "lat": la, "lon": lo, "ts": list(ts)})
    sup.sort(key=lambda c: -c["n"])
    sup = sup[:12]
    home = home_area()
    for c, a in zip(sup, geocode_batch([(c["lat"], c["lon"]) for c in sup])):
        c["area"] = a
    # Bias HARD toward keeping real places: drop ONLY tiny 1-2-frame GPS singletons.
    # A multi-frame cluster is a REAL visit (a true GPS glitch is 1-2 stray frames, not
    # dozens — e.g. a multi-stop road trip). An erased real place is an invisible
    # error; a wrong label a viewer can see and catch. Under-filter, never over-filter —
    # NO distance / time-overlap discount.
    kept, discounted = [], []
    for c in sup:
        why = "tiny" if c["n"] < MIN_CLUSTER_FRAMES else None
        c["home"] = bool(c.get("area") and home and c["area"] == home)
        c["mid"] = sorted(c["ts"])[len(c["ts"]) // 2] if c["ts"] else 0
        c["key"] = f"{round(c['lat'] / GRID)}_{round(c['lon'] / GRID)}"   # stable per place (cluster cache)
        c.pop("ts", None)
        (discounted if why else kept).append(dict(c, **({"why": why} if why else {})))
    clusters = kept[:6]
    areas, seen = [], set()                               # rough chronological order (graduation -> ranch)
    for c in sorted(clusters, key=lambda x: x.get("mid", 0)):
        if c["area"] and c["area"] not in seen:
            seen.add(c["area"]); areas.append(c["area"])
    trips = [a for a in areas if a != home]
    discounted_out = [{"area": d["area"], "n": d["n"], "why": d["why"]} for d in discounted]
    no_gps = sum(1 for r in sel if r["gps_lat"] is None)

    return {"level": level, "label": _label(level, y, m, d, wk), "frames": frames,
            "photos": frames - clips, "clips": clips, "live_photos": live,
            "longest_clip_s": longest, "tod": tod, "cameras": cameras,
            "busiest_day": busiest, "spike": spike, "clusters": clusters,
            "areas": areas, "trips": trips, "home_area": home, "no_gps": no_gps,
            "n_places": len(clusters), "discounted": discounted_out}


# --- LAYER 2a: venue per cluster (Places; cached library-wide) ---------------
def cluster_venue(cluster_key, lat, lon):
    """Resolve (and cache) the nearest establishment for a cluster. Returns
    (venue_name_or_None, venue_lat_or_None, venue_lon_or_None). Cache is the raw Places
    answer; whether to actually USE the name (distance gate / home suppression) is decided
    at assembly. Old cache rows predate coordinate capture -> their coords come back None."""
    with _dlock:
        r = _db.execute("SELECT venue, venue_lat, venue_lon FROM clusters WHERE cluster_key=?",
                        (cluster_key,)).fetchone()
    if r is not None:
        return r[0], r[1], r[2]                  # looked up before (named once = named everywhere)
    if not places_key():
        return None, None, None                  # no key -> don't cache; retry when key added
    venue, vlat, vlon = _places_lookup(lat, lon)
    with _dlock:
        _db.execute("INSERT OR REPLACE INTO clusters"
                    "(cluster_key,lat,lon,venue,venue_lat,venue_lon,generated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (cluster_key, lat, lon, venue, vlat, vlon, int(time.time())))
        _db.commit()
    return venue, vlat, vlon


def _places_lookup(lat, lon):
    """Google Places API (New) — places:searchNearby. Provider is swappable (this is the
    only function to change for a different vendor). Requires 'Places API (New)' enabled."""
    generic = {"locality", "political", "route", "street_address", "postal_code",
               "neighborhood", "sublocality", "administrative_area_level_1",
               "administrative_area_level_2"}
    try:
        import requests
        body = {"maxResultCount": 10, "rankPreference": "DISTANCE",
                "locationRestriction": {"circle": {
                    "center": {"latitude": lat, "longitude": lon}, "radius": VENUE_SEARCH_M}}}
        with _places_sem:
            resp = requests.post(
                "https://places.googleapis.com/v1/places:searchNearby",
                headers={"Content-Type": "application/json",
                         "X-Goog-Api-Key": places_key(),
                         "X-Goog-FieldMask": "places.displayName,places.types,"
                                             "places.primaryType,places.location"},
                json=body, timeout=8)
        data = resp.json()
        if resp.status_code != 200:                      # surface the exact API error (no key)
            _plog(f"Places(New) HTTP {resp.status_code}: "
                  f"{json.dumps(data.get('error', data))[:220]}")
            return None, None, None
        junk = ("bathroom", "restroom", "parking", " atm", "entrance", "exit",
                "lobby", "elevator", "escalator", "stairwell", "hallway", "vending")
        for pl in (data.get("places") or []):            # nearest REAL establishment, skipping sub-features
            name = (pl.get("displayName") or {}).get("text")
            types = set(pl.get("types") or []) | ({pl.get("primaryType")} if pl.get("primaryType") else set())
            if not name or (types & generic):
                continue
            if any(j in name.lower() for j in junk):
                continue
            loc = pl.get("location") or {}               # the Place's own coordinate, for the gate
            return name, loc.get("latitude"), loc.get("longitude")
        return None, None, None
    except Exception as e:
        _plog(f"Places(New) exception: {type(e).__name__}: {str(e)[:80]}")
        return None, None, None


# --- LAYER 2b: prose (Anthropic Haiku; cached per period) --------------------
_SYS = ("You write a one- to two-sentence caption for a personal photo period. "
        "Use ONLY the facts and place names provided. Invent nothing — no events, holidays, "
        "emotions, or places not listed. Warm but factual, plain and specific, not flowery. "
        "No hashtags, no lists, no preamble — output only the caption.\n"
        "VOCABULARY (photo-roll motif): the collective noun for the items in a period — stills "
        "and videos together — is \"frames.\" Never use \"photos,\" \"pictures,\" \"images,\" "
        "\"shots,\" or \"snaps\" as the collective. Use \"photos\" ONLY when explicitly "
        "contrasting stills against \"clips\" (videos). "
        "Examples: \"412 frames across three days\"; \"mostly photos, a handful of clips.\"")


def generate_prose(facts, venues):
    if not anthropic_key():
        return None
    payload = {k: facts.get(k) for k in
               ("label", "frames", "photos", "clips", "live_photos", "tod",
                "cameras", "busiest_day", "areas", "trips")}
    payload["venues"] = venues
    body = {"model": llm_model(), "max_tokens": 150, "system": _SYS,
            "messages": [{"role": "user",
                          "content": "Facts (JSON):\n" + json.dumps(payload) + "\n\nWrite the caption."}]}
    try:
        import requests
        with _llm_sem:
            resp = requests.post("https://api.anthropic.com/v1/messages",
                                 headers={"x-api-key": anthropic_key(),
                                          "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"},
                                 json=body, timeout=20)
        data = resp.json()
        text = "".join(p.get("text", "") for p in (data.get("content") or [])
                       if p.get("type") == "text").strip()
        return text or None
    except Exception:
        return None


# --- orchestration ----------------------------------------------------------
def period_key(level, y, m, d, wk):
    if level == "day":
        return f"d:{y}-{m}-{d}"
    if level == "week":
        return f"w:{y}-{m}-{wk}"
    return f"m:{y}-{m}"


def get_summary(level, y, m, d=None, wk=None, upgrade=False):
    key = period_key(level, y, m, d, wk)
    with _dlock:
        row = _db.execute("SELECT facts_json, venues_json, prose FROM summaries "
                          "WHERE period_key=?", (key,)).fetchone()
    if row and row[2]:                                   # fully cached (has prose)
        return {"level": level, "key": key, "facts": json.loads(row[0]),
                "venues": json.loads(row[1] or "[]"), "prose": row[2],
                "ready": True, "cached": True}
    facts = facts_for(level, y, m, d, wk)
    if not upgrade:
        return {"level": level, "key": key, "facts": facts,
                "venues": [], "prose": None, "ready": False}
    with _gen_sem:                                       # capped upgrade
        with _dlock:
            row = _db.execute("SELECT prose, venues_json FROM summaries WHERE period_key=?",
                              (key,)).fetchone()
        if row and row[0]:
            return {"level": level, "key": key, "facts": facts,
                    "venues": json.loads(row[1] or "[]"), "prose": row[0],
                    "ready": True, "cached": True}
        venues = []
        hm = _home_model()
        for c in facts["clusters"]:
            # HOME SUPPRESSION (backstop): a cluster in a home suburb -- or sitting right on the
            # home centroid -- is named by AREA, never by the nearest shop. "When you're home,
            # the answer is home." Home is the couple of adjacent suburbs of daily life; OTHER
            # suburbs (e.g. a one-off event ~15 km out) are not home and keep their real venues.
            # Skipping here also corrects already-cached home venues without re-calling Places.
            if (c["area"] in hm["suburbs"]
                    or (hm["lat"] and _haversine(hm["lat"], hm["lon"], c["lat"], c["lon"])
                        <= HOME_VENUE_RADIUS_KM)):
                continue
            v, vlat, vlon = cluster_venue(c["key"], c["lat"], c["lon"])
            if not v:
                continue
            # DISTANCE GATE (primary): keep the venue only if the resolved Place sits within
            # VENUE_GATE_M of the cluster centroid (a place you stood in coincides with the
            # photos; a nearest-business-to-the-cluster sits farther). Old cache rows have no
            # coordinate -> nothing to gate on; those are caught by home suppression instead.
            if (vlat is not None and vlon is not None
                    and _haversine(c["lat"], c["lon"], vlat, vlon) * 1000.0 > VENUE_GATE_M):
                continue
            venues.append({"area": c["area"], "venue": v, "n": c["n"], "home": c["home"]})
        prose = generate_prose(facts, [v["venue"] for v in venues])
        if prose:                                        # only cache a real upgrade
            with _dlock:
                _db.execute("INSERT OR REPLACE INTO summaries VALUES(?,?,?,?,?)",
                            (key, json.dumps(facts), json.dumps(venues), prose, int(time.time())))
                _db.commit()
        return {"level": level, "key": key, "facts": facts, "venues": venues,
                "prose": prose, "ready": bool(prose), "cached": False}


def status():
    return {"anthropic_key": bool(anthropic_key()), "places_key": bool(places_key()),
            "model": llm_model(), "cache_db": CACHE_DB}
