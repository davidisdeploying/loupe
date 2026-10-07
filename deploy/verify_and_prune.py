#!/usr/bin/env python3
"""Bounded local retention; Array history is never removed. Dry run by default."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

NAME = re.compile(r'20\d{6}T\d{6}Z')

def validate_archive_mount(local_root, mirror, expected_source):
    def mounted(path):
        raw=subprocess.check_output(['findmnt','--json','-T',str(path),'-o','SOURCE,FSTYPE,TARGET'],text=True,timeout=10)
        return json.loads(raw).get('filesystems',[])
    archive=[m for m in mounted(mirror) if m.get('fstype')=='cifs' and m.get('source')==expected_source
             and (mirror.resolve()==Path(m['target']).resolve() or Path(m['target']).resolve() in mirror.resolve().parents)]
    if not archive:
        raise ValueError('expected independent Array CIFS mount is not active')
    if any(m.get('source')==expected_source for m in mounted(local_root)):
        raise ValueError('local backups are on the archive filesystem')
    return archive[-1]

def generations(root):
    out = []
    for p in root.iterdir():
        if p.is_symlink() or not p.is_dir() or not NAME.fullmatch(p.name):
            continue
        try:
            datetime.strptime(p.name, '%Y%m%dT%H%M%SZ')
        except ValueError:
            continue
        out.append(p)
    return sorted(out, key=lambda p: p.name)

def inventory(root):
    files = {}
    for p in root.rglob('*'):
        if p.is_symlink():
            raise ValueError('symlink in generation')
        if p.is_file():
            files[str(p.relative_to(root))] = p.stat().st_size
    return files

def plan(root, now, keep_days=3, max_generations=2, max_bytes=4*1024**3):
    ordered = generations(root)
    protected = {p.name for p in ordered[-2:]}
    selected, size = [], 0
    for p in ordered:
        if p.name in protected or p.stat().st_mtime >= now-keep_days*86400:
            continue
        n = sum(inventory(p).values())
        if len(selected) >= max_generations or size+n > max_bytes:
            break
        selected.append(p.name)
        size += n
    return {'protected': sorted(protected), 'selected': selected, 'bytes': size}

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()

def verify_pair(a,b):
    if a.is_symlink() or b.is_symlink() or not b.is_dir():
        raise ValueError('missing or symlinked Array generation')
    files=inventory(a)
    if files != inventory(b):
        raise ValueError('Array inventory mismatch')
    manifest=json.loads((a/'manifest.json').read_text())
    if not manifest.get('records') or any(r.get('quick_check') not in {'ok','pg_restore --list ok'} for r in manifest['records']):
        raise ValueError('local generation lacks successful integrity receipt')
    hashes={}
    for rel in sorted(files):
        left,right=digest(a/rel),digest(b/rel)
        if left != right:
            raise ValueError('Array payload mismatch: '+rel)
        hashes[rel]=left
    for r in manifest['records']:
        rel=r.get('backup');expected=r.get('sha256')
        if not isinstance(rel,str) or rel not in hashes or not isinstance(expected,str) or not re.fullmatch(r'[0-9a-f]{64}',expected):
            raise ValueError('invalid manifest payload identity')
        if hashes[rel] != expected:
            raise ValueError('payload differs from original integrity manifest: '+rel)
    return hashes

def apply(root, mirror, selected, now, keep_days=3, record=None):
    if root.resolve() == mirror.resolve() or root.resolve() in mirror.resolve().parents or mirror.resolve() in root.resolve().parents:
        raise ValueError("local and Array roots must be disjoint")
    verified=[]
    for name in selected:
        current=plan(root,now,keep_days)
        if name not in current['selected']:
            raise ValueError('generation became protected/ineligible')
        hashes=verify_pair(root/name,mirror/name)
        verified.append((name,hashes))
        if record is not None:
            record({'name':name,'hashes':hashes,'phase':'verified-before-batch-delete'})
    removed=[]
    for name,hashes in verified:
        if name in {p.name for p in generations(root)[-2:]}:
            raise ValueError('generation became protected')
        shutil.rmtree(root/name)
        if record is not None:
            record({'name':name,'phase':'deleted-local-array-preserved'})
        removed.append({'name':name,'verified_files':len(hashes),'hashes':hashes})
    return removed

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--mirror',type=Path,required=True)
    p.add_argument('--apply',action='store_true')
    p.add_argument('--expected-source',required=True)
    p.add_argument('--check-archive-mount',action='store_true')
    p.add_argument('--keep-days',type=int,default=3)
    p.add_argument('--max-generations',type=int,default=2)
    p.add_argument('--max-bytes',type=int,default=4*1024**3)
    p.add_argument('--receipt',type=Path)
    args=p.parse_args()
    if args.keep_days<3 or not 1<=args.max_generations<=2 or not 1<=args.max_bytes<=4*1024**3:
        p.error('retention must preserve >=3 days and remain bounded to <=2 generations/4GiB')
    mount=validate_archive_mount(args.root,args.mirror,args.expected_source)
    if args.check_archive_mount:
        print(json.dumps({'archive_mount':mount}));raise SystemExit(0)
    if args.receipt is None:
        p.error('--receipt is required for retention planning/apply')
    now=time.time(); result=plan(args.root,now,args.keep_days,args.max_generations,args.max_bytes)
    result.update({'archive_mount':mount,'mode':'apply' if args.apply else 'dry-run','timestamp_utc':datetime.now(timezone.utc).isoformat(),'array_history_preserved':True})
    args.receipt.parent.mkdir(parents=True,exist_ok=True)
    def save():
        temp=args.receipt.with_suffix('.tmp')
        with temp.open('w') as stream:
            stream.write(json.dumps(result,indent=2)+'\n')
            stream.flush();os.fsync(stream.fileno())
        os.replace(temp,args.receipt)
    def record(event):
        result.setdefault('events',[]).append(event);save()
    save()
    if args.apply:
        result['removed']=apply(args.root,args.mirror,result['selected'],now,args.keep_days,record)
        save()
    print(json.dumps({k:v for k,v in result.items() if k!='removed'}))
