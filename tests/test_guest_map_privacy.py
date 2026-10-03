"""W43 home-radius masking must actually mask (audit 9.2, P2.1).

The masking code was written, reviewed and closed as P2.1 -- and it was inert in
production. It masks points near residence CENTROIDS, those centroids are accumulated in
places._rows() from frames whose geocoded city matches a residence's areas, and the
geocoder package was not installed. No cities, no centroids, no centers, nothing to mask
around. `_mask_points_near_residences(pts, [], r)` returns the points unchanged, so a
tunnel guest received the owner's exact coordinates and the precise home centroid.

Nothing errored at any point. The feature was verified once, when residences had just
been saved and the module state happened to be populated, and silently stopped working at
the next restart.

The lesson generalises past this endpoint: a privacy control that depends on upstream data
must fail loudly when that data is missing, not degrade to permitting everything. These
tests assert the masking is OBSERVABLE, not merely present in the source.
"""
import json
import os
import unittest
import urllib.error
import urllib.request

PORT = int(os.environ.get("LOUPE_TEST_PORT", "8000"))
BASE = "http://127.0.0.1:%d" % PORT


def fetch(path, guest=False, timeout=600):
    req = urllib.request.Request(BASE + path)
    if guest:
        # the header the Cloudflare tunnel sets; _is_lan_peer hard-rejects on its presence
        req.add_header("CF-Ray", "privacy-test")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


OWNER = fetch("/api/map/points")
GUEST = fetch("/api/map/points", guest=True)


@unittest.skipUnless(OWNER and GUEST, "map points not reachable")
class GuestMapPrivacy(unittest.TestCase):
    def test_guest_payload_declares_a_mask_radius(self):
        self.assertIn("mask_radius_km", GUEST,
                      "the guest map payload no longer runs through the masking branch")
        self.assertGreater(GUEST["mask_radius_km"], 0)

    def test_residence_centroids_exist_to_mask_around(self):
        """The upstream dependency that silently disabled this. Without centroids the
        mask has nothing to mask around and returns every point untouched."""
        res = fetch("/api/residences")
        self.assertTrue(res, "/api/residences unreachable")
        items = res.get("residences") if isinstance(res, dict) else res
        self.assertTrue(items, "no residences configured")
        # the owner view carries centers; a guest deliberately gets none (P2.1)
        owner_res = fetch("/api/residences")
        oitems = owner_res.get("residences") if isinstance(owner_res, dict) else owner_res
        self.assertTrue(any(r.get("center") for r in oitems),
                        "no residence has a computed centroid -- home masking is inert, "
                        "which is how it failed silently before 2026-08-09")

    def test_guest_coordinates_are_not_the_owner_set(self):
        """The observable property. If these are identical, the mask did nothing."""
        o = {(p["lat"], p["lng"]) for p in OWNER["points"]}
        g = {(p["lat"], p["lng"]) for p in GUEST["points"]}
        self.assertNotEqual(o, g,
                            "guest receives the owner's exact coordinate set -- masking "
                            "is present in the code but not taking effect")

    def test_home_flag_is_populated(self):
        """is_home drives hide-home and away-only bursts as well as this. All of it rides
        on the same residence centroids."""
        home = sum(1 for p in OWNER["points"] if p.get("home"))
        self.assertGreater(home, 0,
                           "no point is flagged home -- is_home is dead, and with it "
                           "hide-home, trip detection and away-only bursts")


if __name__ == "__main__":
    unittest.main(verbosity=2)
