#!/usr/bin/env bash
# Loupe quality harness (P6). stdlib unittest only -- see tests/README.md.
#   tests/run.sh            all
#   tests/run.sh static     source-structure invariants only (no service, no NAS)
#   tests/run.sh data       synthetic-fixture invariants only
#   LOUPE_TEST_SLOW=1 tests/run.sh   also verify the ledger archive contents
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${LOUPE_PYTHON:-/data/loupe-venv/bin/python}"
[[ -x "$PY" ]] || PY=python3

case "${1:-all}" in
  static) PAT="test_static_invariants.py" ;;
  data)   PAT="test_data_invariants.py" ;;
  api)    PAT="test_api_contract.py" ;;
  backup) PAT="test_backup_rail.py" ;;
  deps)   PAT="test_dependencies.py" ;;
  golden) PAT="test_embedding_golden.py" ;;
  schema) PAT="test_schema.py" ;;
  css)    PAT="test_css_tokens.py" ;;
  port)   PAT="test_portability.py" ;;
  cover)  PAT="test_backup_coverage.py" ;;
  stage)  PAT="test_stage_liveness.py" ;;
  ovl)    PAT="test_overlays.py" ;;
  prov)   PAT="test_provider.py" ;;
  s2b)    PAT="test_stage2b_paths.py" ;;
  rel)    PAT="test_release_boundary.py" ;;
  cov)    PAT="test_coverage_report.py" ;;
  route)  PAT="test_routing.py" ;;
  space)  PAT="test_three_spaces.py" ;;
  keys)   PAT="test_keymap.py" ;;
  orient) PAT="test_orientation.py" ;;
  console) PAT="test_console_progress.py" ;;
  lazy)   PAT="test_lazy_imports.py" ;;
  privacy) PAT="test_guest_map_privacy.py" ;;
  proof)  PAT="test_proof_sheet.py" ;;
  pwa)    PAT="test_pwa.py" ;;
  all)    PAT="test_*.py" ;;
  *) echo "usage: run.sh [all|static|data|api|backup|deps|golden|schema|css|port|cover|stage|ovl|prov|s2b|rel|cov|route|space|keys|orient|console|lazy|privacy|proof|pwa]" >&2; exit 2 ;;
esac

cd "$HERE"
"$PY" -m unittest discover -s . -p "$PAT" -v
