"""Exact selected-file residency contract, not a physical-fragmentation reset."""
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'tools/evalscope'))
from file_cache import model_files, observe_files
from run_budget_experiment import prepare_target_cache, payload_residual


def expected_vector(paths, selected):
    selected=set(map(str,selected))
    page=os.sysconf('SC_PAGE_SIZE')
    return {str(p): ((p.stat().st_size+page-1)//page)*page if str(p) in selected else 0
            for p in paths if p.suffix in ('.safetensors','.bin')}


def check_vector(observation, expected):
    if not observation['complete_file_set_observed']:
        raise RuntimeError('cache observation incomplete')
    actual={f['path']:f['resident_bytes'] for f in observation['files']
            if Path(f['path']).suffix in ('.safetensors','.bin')}
    if actual!=expected:
        differences={p:[expected.get(p),actual.get(p)]
                     for p in expected.keys()|actual.keys() if expected.get(p)!=actual.get(p)}
        raise RuntimeError('cache vector differs: '+json.dumps(differences))
    return actual


def prepare(model, output, warm, repair=False, limit_gib=64):
    paths=model_files(model)
    selected=sorted(model.glob('layer-*-experts-*.safetensors')) if warm else []
    if warm and limit_gib == 48:
        prefix=[]
        used=0
        for path in selected:
            if used+path.stat().st_size>48<<30:break
            prefix.append(path);used+=path.stat().st_size
        selected=prefix
    if limit_gib not in (48,64):raise ValueError('unfrozen cache profile')
    if warm and (not selected or sum(p.stat().st_size for p in selected)>limit_gib<<30):
        raise RuntimeError('warm file selection outside frozen budget')
    def save(name,data):(output/name).write_text(json.dumps(data,indent=2)+'\n')
    save('cache-before.json',observe_files(paths))
    errors,after,attempts=prepare_target_cache(paths)
    save('cache-preparation.json',dict(errors=errors,attempts=attempts))
    if errors or payload_residual(after)!=0:
        raise RuntimeError('zero payload prerequisite failed')
    expected=expected_vector(paths,selected)
    save('cache-expected.json',expected)
    deadline=time.monotonic()+180
    buffer=bytearray(4<<20)
    total=0
    for path in selected:
        with path.open('rb',buffering=0) as stream:
            os.posix_fadvise(stream.fileno(),0,0,os.POSIX_FADV_SEQUENTIAL)
            while True:
                if time.monotonic()>deadline:raise TimeoutError('prewarm deadline')
                count=stream.readinto(buffer)
                if not count:break
                total+=count
    save('prewarm.json',dict(bytes=total,files=[str(p) for p in selected]))
    after=observe_files(paths)
    save('cache-ready.json',after)
    if repair:
        if not after['complete_file_set_observed']:
            raise RuntimeError('unknown cache state cannot be repaired')
        actual={f['path']:f['resident_bytes'] for f in after['files']
                if Path(f['path']).suffix in ('.safetensors','.bin')}
        if actual.keys()!=expected.keys() or any(actual[p]>expected[p] for p in expected):
            raise RuntimeError('unexpected resident pages cannot be repaired')
        missing=[Path(p) for p in expected if actual[p]<expected[p]]
        if len(missing)>2 or sum(p.stat().st_size for p in missing)>1<<30:
            raise RuntimeError('missing-file repair exceeds two files / 1GiB')
        save('cache-repair-plan.json',dict(files=[str(p) for p in missing],
            bytes=sum(p.stat().st_size for p in missing),passes=1))
        for path in missing:
            with path.open('rb',buffering=0) as stream:
                while True:
                    if time.monotonic()>deadline:raise TimeoutError('repair deadline')
                    if not stream.readinto(buffer):break
        after=observe_files(paths)
        save('cache-after-repair.json',after)
    check_vector(after,expected)
    save('system-ready.json',{p:Path(p).read_text() for p in
         ('/proc/meminfo','/proc/vmstat','/proc/buddyinfo')})
    return dict(payload_bytes=sum(expected.values()),file_vector_exact=True,
                physical_fragmentation_equal=False)
