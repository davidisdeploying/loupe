"""Every database in the ledger snapshot must also have an off-host copy.

Two independent backup paths exist and they had drifted apart:

  * `tools/ledger_snapshot.sh` writes a consistent tarball of 11 sidecar databases to
    the NAS. Survives losing Charlie, not losing the NAS.
  * `deploy/loupe_data_backup.py` mirrors a set of databases to delta. Survives
    losing the NAS.

On 2026-08-09 five databases were in the first list and not the second -- `vault.db`,
`edits.db`, `renders.db`, `pairs.db`, `summaries.db`. Their only off-host copy was the
ledger tarball, which lives on the NAS, so losing the NAS would have taken vault marks
and edits with it. Those two are direct human decisions and are not regenerable by
re-running anything, which is the entire premise of the ledger.

This test is the reason that cannot happen quietly again: adding a database to the
ledger without giving it an off-host home now fails here.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LEDGER = os.path.join(REPO, "tools", "ledger_snapshot.sh")
BACKUP = os.path.join(REPO, "deploy", "loupe_data_backup.py")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def ledger_databases():
    m = re.search(r"DBS=\(\s*(.*?)\n\)", read(LEDGER), re.S)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        line = line.split("#")[0].strip()
        if line.endswith(".db"):
            out.append(os.path.basename(line))
    return out


def backup_databases():
    src = read(BACKUP)
    m = re.search(r"SOURCES\s*=\s*\{(.*?)\n\}", src, re.S)
    if not m:
        return set()
    names = re.findall(r'"([^"]+\.db)"', m.group(1))
    return {os.path.basename(n) for n in names}


@unittest.skipUnless(os.path.exists(LEDGER) and os.path.exists(BACKUP),
                     "backup scripts not present")
class BackupCoverage(unittest.TestCase):
    def test_ledger_lists_databases(self):
        self.assertGreaterEqual(len(ledger_databases()), 11,
                                "could not parse the ledger DBS list")

    def test_every_ledger_database_has_an_offhost_copy(self):
        ledger = ledger_databases()
        offhost = backup_databases()
        missing = sorted(d for d in ledger if d not in offhost)
        self.assertEqual(
            missing, [],
            "in the NAS ledger but with no off-host copy: %s\n"
            "Their only surviving copy would be the ledger tarball, which is on the NAS. "
            "Add them to SOURCES in deploy/loupe_data_backup.py." % missing)

    def test_the_human_decision_databases_are_covered(self):
        """Named explicitly so a failure says what is at stake rather than listing a set.

        decisions/vault/edits are keep-cut calls, vault marks and edits: hundreds of
        hours of judgment that no pipeline re-run reproduces."""
        offhost = backup_databases()
        for db in ("decisions.db", "vault.db", "edits.db"):
            self.assertIn(db, offhost,
                          "%s holds irreproducible human decisions and has no off-host copy" % db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
