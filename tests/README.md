# Loupe quality harness (P6)

Executable invariants, golden contracts, and regression tests for the two real
outages found on 2026-08-09. **stdlib only** — deliberately no pytest, because the
only interpreter is the production venv `/data/loupe-venv` and adding a test
dependency there would change `deploy/locks/loupe-venv.lock`.

    tests/run.sh              # everything
    tests/run.sh static       # no running service, no NAS needed
    tests/run.sh data         # synthetic fixtures only

## What each file guards

| file | guards |
|---|---|
| `test_static_invariants.py` | Structure of `server.py` itself — every write route is behind the gate, the gate is *first*, `_is_lan_peer` still rejects Cloudflare headers, token compare is constant-time, preview writes still trigger eviction. Needs nothing running. |
| `test_data_invariants.py` | Faithful-library rules against synthetic fixtures — `EXCLUDE_SQL` must not silently drop assets (DL-L2), `ro()` really is read-only, `VIDEO_EXT` shape. |
| `test_api_contract.py` | Live service: golden response shapes plus the full P2.2 security matrix, so the write gate can never quietly regress. |
| `test_backup_rail.py` | The Aug 6–9 outage as a test: asserts a *recent file exists in the destination*, not that the unit exited 0. |

## Why the static tests parse source instead of importing

Importing `server.py` opens databases, initializes the faces and search modules and
pulls in the ML stack. The structural invariants worth protecting are syntactic —
"is there a route that skips the gate?" — so `ast` answers them faster and without a
live environment. A 17th POST route added above the guard fails `test_static_invariants`
on a laptop with no NAS mounted.
