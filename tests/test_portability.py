"""Portability invariants (P7 / W16).

Loupe is meant to be installable somewhere other than this house. The app side already
resolves the media root from `LIBRARY_ROOT`; these tests stop new hard-coded mount points
from creeping back into the code that would have to move with it.

The allowlist is deliberate and small. Everything on it is explained -- an unexplained
entry defeats the point.
"""
import ast
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Literal mount points that must not appear in portable code.
MOUNTS = re.compile(r"/mnt/nas\d*")

# Paths whose literals are legitimate, each with the reason.
ALLOW = {
    # One-shot migrations that already ran against this house's NAS. De-hardcoding a
    # script that must never run again would imply it is re-runnable, which is worse.
    "pipeline/culling/": "historical one-shot migrations, already executed",
    # Synthetic fixture rows -- they are literals on purpose, standing in for real ones.
    "tests/test_data_invariants.py": "synthetic fixture data",
    "tests/test_portability.py": "this file names the patterns it forbids",
    # An example value shown in a form field, not a path any code resolves.
    "setup_page.py": "UI placeholder text, not a resolved path",
    # Documentation describing this deployment is supposed to name its real paths.
    "OPERATIONS.md": "documentation of this deployment",
    "ONBOARDING.md": "documentation of this deployment",
}

# The sanctioned pattern: a literal as the DEFAULT of an environment lookup. Both
# languages count -- shell parameter expansion ${VAR:-/mnt/...} is the same contract as
# Python's os.environ.get(..., "/mnt/..."), and an earlier version of this test only
# recognised the Python form and so flagged tools/ledger_snapshot.sh as an offender.
ENV_DEFAULT = re.compile(
    r"environ\.get\([^)]*\)"                      # python: os.environ.get(...)
    r"|os\.path\.join\(os\.sep,\s*[\"']mnt[\"']"  # python: os.path.join(os.sep, "mnt", ...)
    r"|\$\{[A-Za-z_][A-Za-z0-9_]*:-"                # shell:  ${VAR:-/mnt/...}
)


def source_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".git", ".insightface", "archive-pre-delete",
                                "node_modules", "deploy")]
        for f in files:
            if f.endswith((".py", ".sh")):
                yield os.path.relpath(os.path.join(root, f), REPO)


def docstring_lines(src):
    """Every 1-based line number occupied by a docstring, via an ast walk.

    Replaces the old quote-substring heuristic, which only recognised the line
    carrying the opening quotes and so reported continuation lines *inside* a
    multi-line docstring as hard-coded paths. Prose describing the deployment is allowed to
    name real paths; only executable lines are held to the env-with-default form.
    A file that does not parse yields no prose lines, so it is checked strictly
    rather than silently skipped.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def allowed(rel):
    return any(rel.startswith(k) or rel == k for k in ALLOW)


class NoHardCodedMounts(unittest.TestCase):
    def test_no_new_hardcoded_mount_points(self):
        offenders = []
        for rel in sorted(source_files()):
            if allowed(rel):
                continue
            path = os.path.join(REPO, rel)
            try:
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            prose = docstring_lines(src) if rel.endswith(".py") else set()
            for i, line in enumerate(src.splitlines(), 1):
                if not MOUNTS.search(line):
                    continue
                if ENV_DEFAULT.search(line):
                    continue                      # the sanctioned env-with-default form
                if line.lstrip().startswith("#") or i in prose:
                    continue                      # comments and docstrings may cite reality
                offenders.append("%s:%d %s" % (rel, i, line.strip()[:88]))
        self.assertEqual(
            offenders, [],
            "hard-coded mount points outside the env scheme:\n  " + "\n  ".join(offenders))


class OneDefinitionOfWorkProduct(unittest.TestCase):
    """server.py and pipeline/culling.py both describe the 'production' work-product
    tree. Two hand-written copies of one concept disagree the moment the root moves."""

    def test_both_derive_from_library_root(self):
        with open(os.path.join(REPO, "server.py"), encoding="utf-8") as fh:
            srv = fh.read()
        with open(os.path.join(REPO, "pipeline", "culling.py"), encoding="utf-8") as fh:
            cul = fh.read()
        self.assertRegex(srv, r"_prod_prefix\s*=\s*LIBRARY_ROOT",
                         "server.py stopped deriving the production prefix from LIBRARY_ROOT")
        self.assertIn("LIBRARY_ROOT", cul,
                      "pipeline/culling.py stopped deriving its work-product paths")
        self.assertNotIn("'/mnt/nas2/photos/production/%'", cul,
                         "pipeline/culling.py re-hard-coded the work-product path")

    def test_they_agree_under_the_default_root(self):
        import sys
        sys.path.insert(0, os.path.join(REPO, "pipeline"))
        import culling
        default_root = os.path.join(os.sep, "mnt", "nas2", "photos")
        expected = default_root.rstrip(os.sep) + os.sep + "production" + os.sep + "%"
        self.assertIn(expected, culling.WORKPRODUCT_SQL,
                      "culling.py and server.py no longer describe the same tree")


if __name__ == "__main__":
    unittest.main(verbosity=2)
