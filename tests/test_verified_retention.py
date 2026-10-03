import hashlib,json,os
from pathlib import Path
import tempfile
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"deploy"))
from verify_and_prune import plan, apply, validate_archive_mount

class VerifiedRetentionTests(unittest.TestCase):
    def populate(self,root,mirror):
        for name in ['20260101T000000Z','20260102T000000Z','20260103T000000Z','20260104T000000Z']:
            for parent in [root,mirror]:
                p=parent/name;p.mkdir();(p/'db.sqlite3').write_bytes(b'unchanged fixture')
                (p/'manifest.json').write_text(json.dumps({'records':[{'quick_check':'ok','backup':'db.sqlite3','sha256':hashlib.sha256(b'unchanged fixture').hexdigest()}]}))
                os.utime(p,(1,1))
    def test_eight_digit_dates_keep_two_even_when_every_generation_is_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,mirror=Path(tmp)/'local',Path(tmp)/'mirror';root.mkdir();mirror.mkdir();self.populate(root,mirror)
            x=plan(root,1800000000)
            self.assertEqual(x['selected'],['20260101T000000Z','20260102T000000Z'])
            self.assertEqual(x['protected'],['20260103T000000Z','20260104T000000Z'])
    def test_mismatched_array_payload_prevents_any_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,mirror=Path(tmp)/'local',Path(tmp)/'mirror';root.mkdir();mirror.mkdir();self.populate(root,mirror)
            (mirror/'20260101T000000Z/db.sqlite3').write_bytes(b'corrupted fixture')
            with self.assertRaises(ValueError):apply(root,mirror,['20260101T000000Z'],1800000000)
            self.assertTrue((root/'20260101T000000Z').exists())
    def test_verified_deletion_is_only_local_and_never_touches_latest_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,mirror=Path(tmp)/'local',Path(tmp)/'mirror';root.mkdir();mirror.mkdir();self.populate(root,mirror)
            apply(root,mirror,['20260101T000000Z'],1800000000)
            self.assertFalse((root/'20260101T000000Z').exists())
            self.assertTrue((mirror/'20260101T000000Z/db.sqlite3').exists())
            self.assertEqual(len(list(root.iterdir())),3)
    def test_recent_invalid_and_oversized_generations_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,mirror=Path(tmp)/'local',Path(tmp)/'mirror';root.mkdir();mirror.mkdir();self.populate(root,mirror)
            (root/'20261340T999999Z').mkdir();(root/'operator-notes').mkdir()
            os.utime(root/'20260101T000000Z',(1800000000,1800000000))
            self.assertEqual(plan(root,1800000000,max_generations=1)['selected'],['20260102T000000Z'])
            self.assertEqual(plan(root,1800000000,max_bytes=1)['selected'],[])

    def test_identically_corrupted_local_and_array_payloads_fail_original_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,mirror=Path(tmp)/'local',Path(tmp)/'mirror';root.mkdir();mirror.mkdir();self.populate(root,mirror)
            for parent in [root,mirror]:
                (parent/'20260101T000000Z/db.sqlite3').write_bytes(b'corrupted fixture')
            with self.assertRaisesRegex(ValueError,'original integrity manifest'):
                apply(root,mirror,['20260101T000000Z'],1800000000)
            self.assertTrue((root/'20260101T000000Z').exists())

    def test_local_shadow_archive_is_rejected(self):
        from unittest.mock import patch
        with patch('verify_and_prune.subprocess.check_output',return_value=json.dumps({'filesystems':[{'source':'/dev/local','fstype':'ext4','target':'/'}]})):
            with self.assertRaisesRegex(ValueError,'independent Array'):
                validate_archive_mount(Path('/local'),Path('/archive'), '//nas/share')
    def test_real_separate_cifs_archive_is_accepted(self):
        from unittest.mock import patch
        outputs=[json.dumps({'filesystems':[{'source':'//nas/share','fstype':'cifs','target':'/archive'}]}),json.dumps({'filesystems':[{'source':'/dev/local','fstype':'ext4','target':'/'}]})]
        with patch('verify_and_prune.subprocess.check_output',side_effect=outputs):
            self.assertEqual(validate_archive_mount(Path('/local'),Path('/archive/generations'),'//nas/share')['fstype'],'cifs')

    def test_second_generation_failure_preserves_entire_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,mirror=Path(tmp)/'local',Path(tmp)/'mirror';root.mkdir();mirror.mkdir();self.populate(root,mirror)
            (mirror/'20260102T000000Z/db.sqlite3').write_bytes(b'corrupted fixture')
            with self.assertRaises(ValueError):
                apply(root,mirror,['20260101T000000Z','20260102T000000Z'],1800000000)
            self.assertTrue((root/'20260101T000000Z').exists())
            self.assertTrue((root/'20260102T000000Z').exists())
