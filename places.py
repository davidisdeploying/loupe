#!/usr/bin/env python3
"""
places.py — geolocation data layer for loupe's Places feature. READ-ONLY on
metadata.db + apple-enrichment.db; REUSES summaries.py for the home model
(centroid + suburb suppression) and the cached, distance-gated Google-Places
venue names. No per-period cluster stitching — fresh global pass over GPS assets.

Computed once and cached in-process (the data is static for a session):
  payload()  -> compact per-asset rows [id, lat, lon, year, score, home_flag]
  venues()   -> gated, non-home venue points [name, lat, lon] for client labelling
  trips()    -> global away-from-home journeys (review units for the trips rail)
  bursts()   -> place-burst cull candidates (one venue, one day, heavy frame count)
"""
import os, sqlite3, time
from collections import defaultdict

import summaries as SUM   # reuse: _home_model(), _haversine(), venue cache, gates

from loupe_common import APP_DATA, METADATA_DB as META, EXCLUDE_SQL, VIDEO_EXT, ro as _ro
ENRICH = os.path.join(APP_DATA, "apple-enrichment.db")
SUMM = os.path.join(APP_DATA, "summaries.db")

HOME_HIDE_KM = SUM.HOME_METRO_KM          # 8 km — "hide home" de-blobs the daily-life metro
HOME_TIGHT_KM = SUM.HOME_VENUE_RADIUS_KM  # 2.5 km — venue/burst home backstop
VENUE_GATE_M = SUM.VENUE_GATE_M           # 200 m — only label a cluster with a venue this close
TRIP_AWAY_KM = 60.0                       # a JOURNEY leaves the metroplex (local outings aren't trips)
TRIP_GAP_DAYS = 2                         # >2-day gap between away-frames starts a new journey
TRIP_MIN_FRAMES = 40                      # rail shows substantial journeys, not quick far stops
BURST_CELL_M = 150.0                      # a venue footprint
BURST_FLOOR = int(os.environ.get("LOUPE_BURST_FLOOR", "50"))   # tunable; heavy end only
_DAY = 86400

# --- time-aware home, data-driven: the RESIDENCES store is the single source of truth ---
# A residence = {label, areas[], radius_km, start "YYYY-MM", end "YYYY-MM"|None}. is_home(frame)
# = some residence active at the frame's month whose area-set contains the frame's geocoded
# city OR whose centroid is within radius_km. The SAME predicate feeds trip detection, the
# map's hide-home, and away-only bursts. server.py sets RESIDENCES before first use; no
# hardcoded cutover/regions.
RESIDENCES = []
def _ym(ts):
    if ts is None:
        return None
    lt = time.localtime(ts)
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}"
def _active(res, ym):
    return ym is not None and ym >= res["start"] and (not res.get("end") or ym <= res["end"])

# server.py populates this (the paired Live-Photo motion clips) BEFORE first use, so the
# map shows one dot per Live Photo (not still+mov) and bursts count reviewable frames only.
HIDDEN_MOV_IDS = set()
_cache = {}


def _hav(a, b, c, d):
    return SUM._haversine(a, b, c, d)


def _home():
    h = SUM._home_model()
    return h["lat"], h["lon"]


# --- offline reverse-geocoder naming (the ONLY trip-title source) -------------
# Trip titles are "City, State" (US) / "City, Country" (non-US) straight from the
# offline reverse-geocoder. No Google-Places business cache, no editorial text.
STATE_ABBR = {
 "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA",
 "Colorado":"CO","Connecticut":"CT","Delaware":"DE","Florida":"FL","Georgia":"GA",
 "Hawaii":"HI","Idaho":"ID","Illinois":"IL","Indiana":"IN","Iowa":"IA","Kansas":"KS",
 "Kentucky":"KY","Louisiana":"LA","Maine":"ME","Maryland":"MD","Massachusetts":"MA",
 "Michigan":"MI","Minnesota":"MN","Mississippi":"MS","Missouri":"MO","Montana":"MT",
 "Nebraska":"NE","Nevada":"NV","New Hampshire":"NH","New Jersey":"NJ","New Mexico":"NM",
 "New York":"NY","North Carolina":"NC","North Dakota":"ND","Ohio":"OH","Oklahoma":"OK",
 "Oregon":"OR","Pennsylvania":"PA","Rhode Island":"RI","South Carolina":"SC",
 "South Dakota":"SD","Tennessee":"TN","Texas":"TX","Utah":"UT","Vermont":"VT",
 "Virginia":"VA","Washington":"WA","West Virginia":"WV","Wisconsin":"WI","Wyoming":"WY"}
COUNTRY = {"US":"USA","NL":"Netherlands","BE":"Belgium","MX":"Mexico","CH":"Switzerland",
 "FR":"France","GB":"United Kingdom","DE":"Germany","GH":"Ghana","CN":"China","BR":"Brazil",
 "AU":"Australia","CA":"Canada","IT":"Italy","ES":"Spain","JP":"Japan","IE":"Ireland"}


def _geocode_recs(coords):
    """Offline reverse-geocoder records (name/admin1/cc) — same engine summaries uses."""
    if not coords:
        return []
    try:
        import reverse_geocoder as rg
        return rg.search([(float(a), float(b)) for a, b in coords], mode=1)
    except Exception:
        return [{} for _ in coords]


def _name_parts(rec):
    """(title, city, region, postmark_state) from a geocoder record. title is the
    eyeball 'City, ST'; region is the line beneath the city on the postcard."""
    name = rec.get("name")
    if not name:
        return None, None, None, None
    a1, cc = rec.get("admin1"), rec.get("cc")
    if cc == "US":
        st = STATE_ABBR.get(a1, a1 or "")
        return f"{name}, {st}", name, (a1 or st), st
    ctry = COUNTRY.get(cc, cc or "")
    return f"{name}, {ctry}", name, ctry, (cc or "")


def _rows():
    """All GPS-bearing review-scope assets, with Apple score, sharpness, extension, and
    the offline-geocoded place (name/region/title) — geocoded once, library-wide."""
    if "rows" in _cache:
        return _cache["rows"]
    conn = _ro(META)
    base = conn.execute(
        f"SELECT id, gps_lat lat, gps_lon lon, year, capture_timestamp ts, "
        f"upper(extension) ext, blur_laplacian blur "
        f"FROM assets WHERE gps_lat IS NOT NULL AND gps_lon IS NOT NULL AND {EXCLUDE_SQL}").fetchall()
    conn.close()
    score = {}
    try:
        en = _ro(ENRICH)
        score = {r[0]: r[1] for r in en.execute(
            "SELECT asset_id, overall FROM apple_score WHERE overall IS NOT NULL")}
        en.close()
    except Exception:
        pass
    recs = _geocode_recs([(r["lat"], r["lon"]) for r in base])
    # pass 1: geocode; accumulate each residence's centroid from frames whose city is in its areas
    rows = []
    acc = [[0.0, 0.0, 0] for _ in RESIDENCES]
    for r, rec in zip(base, recs):
        if r["id"] in HIDDEN_MOV_IDS:        # one dot per Live Photo (the still), never the hidden mov
            continue
        title, city, region, pmstate = _name_parts(rec)
        for k, R in enumerate(RESIDENCES):
            if title in R["areas_set"]:
                a = acc[k]; a[0] += r["lat"]; a[1] += r["lon"]; a[2] += 1
        rows.append((r, title, city, region, pmstate))
    for k, R in enumerate(RESIDENCES):
        a = acc[k]
        R["_cent"] = (a[0] / a[2], a[1] / a[2]) if a[2] else None
    # pass 2: is_home + trip-distance against the residence(s) active at each frame's month
    out = []
    for r, title, city, region, pmstate in rows:
        ym = _ym(r["ts"])
        home = False; bestd = None
        for R in RESIDENCES:
            if not _active(R, ym):
                continue
            d = _hav(R["_cent"][0], R["_cent"][1], r["lat"], r["lon"]) if R["_cent"] else None
            if (title in R["areas_set"]) or (d is not None and d <= R["radius_km"]):
                home = True
            if d is not None and (bestd is None or d < bestd):
                bestd = d
        if bestd is None:                      # gap / centroid-less active residence: measure to nearest home in time
            ref = _nearest_res_centroid(ym)
            bestd = _hav(ref[0], ref[1], r["lat"], r["lon"]) if ref else 0.0
        out.append({"id": r["id"], "lat": r["lat"], "lon": r["lon"], "year": r["year"],
                    "ts": r["ts"], "score": score.get(r["id"]), "blur": r["blur"],
                    "still": (r["ext"] or "") not in VIDEO_EXT, "dist": bestd, "home": home,
                    "title": title, "city": city, "region": region, "pmstate": pmstate})
    _cache["rows"] = out
    return out


def set_residences(reslist):
    """Install the residence store (from server) and invalidate caches so is_home / trips /
    map / bursts recompute against the new history on next access."""
    global RESIDENCES
    RESIDENCES = []
    for R in (reslist or []):
        RESIDENCES.append({"label": R.get("label", ""), "areas": list(R.get("areas", [])),
                           "areas_set": set(R.get("areas", [])),
                           "radius_km": float(R.get("radius_km") or 40),
                           "start": R.get("start") or "0000-00", "end": R.get("end") or None,
                           "color": R.get("color"), "id": R.get("id")})
    _cache.clear()


def _nearest_res_centroid(ym):
    """Centroid of the residence whose date-range is nearest (in months) to ym — used to keep
    trip-distance meaningful during gaps with no declared home."""
    best, bestgap = None, None
    def _m(s):
        try: y, mo = s.split("-"); return int(y) * 12 + int(mo)
        except Exception: return None
    t = _m(ym) if ym else None
    for R in RESIDENCES:
        if not R.get("_cent"):
            continue
        s = _m(R["start"]); e = _m(R["end"]) if R.get("end") else 10 ** 9
        gap = 0 if (t is None or (s is not None and s <= t <= e)) else min(abs(t - s) if s else 10 ** 9,
                                                                           abs(t - e) if e < 10 ** 9 else 10 ** 9)
        if bestgap is None or gap < bestgap:
            best, bestgap = R["_cent"], gap
    return best


def place_names():
    """Typeahead source: the library's own geocoded city names with frame counts (real places
    only). Same names is_home matches on, so chips and matching can't drift."""
    if "place_names" in _cache:
        return _cache["place_names"]
    counts = defaultdict(int)
    for r in _rows():
        if r["title"]:
            counts[r["title"]] += 1
    out = [{"name": n, "count": c} for n, c in
           sorted(counts.items(), key=lambda kv: -kv[1])]
    _cache["place_names"] = out
    return out


def payload():
    """Compact rows for the (secondary) map: [id, lat, lon, year|0, score*100|-1, home].
    No venue labels — the basemap supplies geography; trip titles come from the gallery."""
    rows = _rows()
    arr = [[r["id"], round(r["lat"], 5), round(r["lon"], 5), r["year"] or 0,
            (round(r["score"] * 100) if r["score"] is not None else -1),
            1 if r["home"] else 0] for r in rows]
    hlat, hlon = _home()
    return {"assets": arr, "home": {"lat": hlat, "lon": hlon},
            "years": sorted({r["year"] for r in rows if r["year"]}),
            "mappable": len(arr), "total": _total_assets(),
            "hidden_home": sum(1 for r in rows if r["home"])}


def _total_assets():
    conn = _ro(META)
    n = conn.execute(f"SELECT COUNT(*) FROM assets WHERE {EXCLUDE_SQL}").fetchone()[0]
    conn.close()
    return n - len(HIDDEN_MOV_IDS)   # reviewable total (paired Live MOVs aren't separate assets)


def _coord_str(lat, lon):
    return f"{abs(lat):.2f}°{'N' if lat>=0 else 'S'} {abs(lon):.2f}°{'E' if lon>=0 else 'W'}"


def _hero(g):
    """(hero_id, method). Highest Apple score among STILLS; else sharpest still (highest
    blur_laplacian — loupe's convention: high = sharp); else None (neutral well)."""
    stills = [r for r in g if r["still"]]
    scored = [r for r in stills if r["score"] is not None]
    if scored:
        return max(scored, key=lambda r: r["score"])["id"], "scored"
    blurred = [r for r in stills if r["blur"] is not None]
    if blurred:
        return max(blurred, key=lambda r: r["blur"])["id"], "sharpness"
    return None, "neutral"


def trips():
    """Global away-from-home journeys, grouped by capture-day contiguity (coarse — mixed-
    timezone capture_timestamp, bucket by DAY only, never the hour). Titled by the trip's
    MODAL city (offline reverse-geocoder, by frame count) — never a business or blurb."""
    if "trips" in _cache:
        return _cache["trips"]
    away = [r for r in _rows() if r["ts"] and r["dist"] > TRIP_AWAY_KM]
    away.sort(key=lambda r: r["ts"])
    groups, cur = [], []
    for r in away:
        if cur and (r["ts"] - cur[-1]["ts"]) > TRIP_GAP_DAYS * _DAY:
            groups.append(cur); cur = []
        cur.append(r)
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        if len(g) < TRIP_MIN_FRAMES:
            continue
        clat = sum(r["lat"] for r in g) / len(g)
        clon = sum(r["lon"] for r in g) / len(g)
        days = sorted({time.strftime("%Y-%m-%d", time.localtime(r["ts"])) for r in g})
        # MODAL city by frame count = the trip's center of gravity
        tally = defaultdict(int)
        rep_of = {}
        for r in g:
            if r["title"]:
                tally[r["title"]] += 1
                rep_of[r["title"]] = r
        if tally:
            mt = max(tally, key=lambda k: tally[k]); rr = rep_of[mt]
            title, city, region, pmstate = rr["title"], rr["city"], rr["region"], rr["pmstate"]
        else:
            title, city, region, pmstate = "Away", "Away", "", ""
        hero, hmethod = _hero(g)
        mon = time.strftime("%b", time.strptime(days[0], "%Y-%m-%d")).upper()
        out.append({
            "title": title, "city": city, "region": region,
            "postmark": {"state": pmstate, "month": mon},
            "frames": len(g), "days": len(days), "start": days[0], "end": days[-1],
            "lat": round(clat, 5), "lon": round(clon, 5), "coord": _coord_str(clat, clon),
            "bounds": [round(min(r["lat"] for r in g), 5), round(min(r["lon"] for r in g), 5),
                       round(max(r["lat"] for r in g), 5), round(max(r["lon"] for r in g), 5)],
            "hero": hero, "hero_method": hmethod, "ids": [r["id"] for r in g]})
    out.sort(key=lambda t: t["start"], reverse=True)
    _cache["trips"] = out
    return out


def _burst_groups():
    if "bursts" in _cache:
        return _cache["bursts"]
    cell = BURST_CELL_M / 111320.0
    groups = defaultdict(list)
    for r in _rows():
        if r["ts"] is None:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(r["ts"]))
        groups[(round(r["lat"] / cell), round(r["lon"] / cell), day)].append(r)
    out = []
    for (_, _, day), g in groups.items():
        if len(g) < BURST_FLOOR:
            continue
        # a burst AT home (residential suburbs) is daily life, not a reviewable outing
        if sum(1 for r in g if r["home"]) > len(g) / 2:
            continue
        clat = sum(r["lat"] for r in g) / len(g)
        clon = sum(r["lon"] for r in g) / len(g)
        # label by offline modal city (never a business cache) — for the report only
        tally = defaultdict(int)
        for r in g:
            if r["title"]:
                tally[r["title"]] += 1
        place = max(tally, key=lambda k: tally[k]) if tally else None
        out.append({"day": day, "n": len(g), "lat": round(clat, 5), "lon": round(clon, 5),
                    "place": place, "ids": [r["id"] for r in g]})
    out.sort(key=lambda b: -b["n"])
    _cache["bursts"] = out
    return out


def burst_ids():
    """The flat set of asset ids in any qualifying place-burst (for the candidate merge)."""
    s = set()
    for b in _burst_groups():
        s.update(b["ids"])
    return s


# ===========================================================================
# Map presentation layer (read-only, cached). REUSES _rows()/trips()/RESIDENCES
# wholesale — no new clustering and NO Places-API calls (offline geocoder only).
# ===========================================================================
def map_points(frm=None, to=None):
    """Geotagged assets for the client-side supercluster:
        {id, lat, lng, t, place, home, y}
      t    = capture epoch (the client's time-scrubber filter; null tolerated)
      place= cached offline-geocoder 'City, ST' (cluster labels; never a venue)
      home = the SAME time-aware is_home flag trips/bursts use (Home-areas layer)
      y    = capture year (cheap scrubber filter, tz-stable). Optional from/to
      year bounds narrow the set server-side for deep-links; the live map loads
      the whole set once and filters locally."""
    key = ("map_points", frm, to)
    if key in _cache:
        return _cache[key]
    out = []
    for r in _rows():
        y = r["year"] or 0
        if frm is not None and y and y < frm:
            continue
        if to is not None and y and y > to:
            continue
        out.append({"id": r["id"], "lat": round(r["lat"], 5), "lng": round(r["lon"], 5),
                    "t": r["ts"], "place": r["title"], "home": 1 if r["home"] else 0,
                    "y": y})
    _cache[key] = out
    return out


def _stops_for_ids(ids):
    """Ordered journey waypoints for a trip's frames: walk the frames in capture
    order and emit one stop per run of the same geocoded place (consecutive
    dedup), each stop the centroid of its run. Gives a clean film-strip polyline
    of the cities visited in sequence."""
    byid = _rows_by_id()
    seq = sorted((byid[i] for i in ids if i in byid),
                 key=lambda r: r["ts"] or 0)
    stops = []
    run = []
    last = object()
    for r in seq:
        key = r["title"] or "·"
        if run and key != last:
            stops.append(_centroid_stop(run, last))
            run = []
        run.append(r); last = key
    if run:
        stops.append(_centroid_stop(run, last))
    return stops


def _centroid_stop(run, place):
    n = len(run)
    return {"place": None if place == "·" else place,
            "lat": round(sum(r["lat"] for r in run) / n, 5),
            "lng": round(sum(r["lon"] for r in run) / n, 5),
            "t": run[len(run) // 2]["ts"], "n": n}


def _rows_by_id():
    if "_rows_by_id" in _cache:
        return _cache["_rows_by_id"]
    m = {r["id"]: r for r in _rows()}
    _cache["_rows_by_id"] = m
    return m


def map_trips(frm=None, to=None):
    """Trips as ordered stops for the overlay polylines. Reuses trips() verbatim;
    adds `stops` (ordered waypoints) and `i` (index into trips(), so the client
    reuses /api/trip_items?i= for review). Optional year overlap filter."""
    key = ("map_trips", frm, to)
    if key in _cache:
        return _cache[key]
    out = []
    for i, t in enumerate(trips()):
        sy = int(t["start"][:4]) if t.get("start") else None
        ey = int(t["end"][:4]) if t.get("end") else sy
        if frm is not None and ey is not None and ey < frm:
            continue
        if to is not None and sy is not None and sy > to:
            continue
        out.append({
            "i": i, "title": t["title"], "city": t.get("city"), "region": t.get("region"),
            "start": t["start"], "end": t["end"], "frames": t["frames"], "days": t["days"],
            "lat": t["lat"], "lng": t["lon"], "bounds": t["bounds"], "hero": t.get("hero"),
            "stops": _stops_for_ids(t["ids"])})
    _cache[key] = out
    return out


def residence_geo():
    """Residence zones for the map: computed centroid + radius + era + style, by
    id. The centroid is the SAME one is_home() uses (R['_cent'], averaged from the
    library's own frames whose city is in the residence's areas)."""
    if "residence_geo" in _cache:
        return _cache["residence_geo"]
    _rows()                              # ensure R['_cent'] is populated
    out = []
    for R in RESIDENCES:
        c = R.get("_cent")
        out.append({"id": R.get("id"), "label": R.get("label"),
                    "radius_km": R.get("radius_km"),
                    "start": R.get("start"), "end": R.get("end"),
                    "color": R.get("color") or "#BA7517",
                    "lat": round(c[0], 5) if c else None,
                    "lng": round(c[1], 5) if c else None})
    _cache["residence_geo"] = out
    return out


def no_gps_count():
    """Review-scope assets with no GPS — 'N have no location and live in Library'.
    The real NULL-GPS row count (matches the metadata.db query exactly)."""
    if "no_gps" in _cache:
        return _cache["no_gps"]
    conn = _ro(META)
    n = conn.execute(
        f"SELECT COUNT(*) FROM assets WHERE gps_lat IS NULL AND {EXCLUDE_SQL}"
    ).fetchone()[0]
    conn.close()
    _cache["no_gps"] = n
    return n


def map_meta():
    """Small companion payload: home center, year span, counts, residences."""
    rows = _rows()
    hlat, hlon = _home()
    years = sorted({r["year"] for r in rows if r["year"]})
    return {"home": {"lat": hlat, "lng": hlon},
            "years": years,
            "span": [years[0], years[-1]] if years else [None, None],
            "mappable": len(rows), "no_gps": no_gps_count(),
            "total": _total_assets(), "residences": residence_geo()}


def bursts_summary():
    g = _burst_groups()
    return {"floor": BURST_FLOOR, "count": len(g), "frames": sum(b["n"] for b in g),
            "sample": [{"place": b["place"], "day": b["day"], "n": b["n"]} for b in g[:8]]}
