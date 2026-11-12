from pathlib import Path
import sys
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.serve_recovery import publication_archive as archive
from tools.serve_service import package as p
W=Path(__file__).resolve().parent;STATE=ROOT/'build/service-native-state'
with archive.locked(STATE) as (state,desc,manifest,current):
    assert current['revision']==110 and current['current']['manifest_sha256']=='eeba689ceb1a0d5df0cd1e351525af5ac1806ca421b9d7cb738ed20393b2fea1'
    pool=ROOT/'build/.q4t-publication/generations'
    paths=sorted(pool.iterdir(),key=lambda f:(f.stat().st_mtime_ns,f.name))
    assert len(paths)==32
    keep=archive.protected(current,archive.intents(state,desc))
    selected=[]
    for path in paths:
        if path.name in keep:continue
        try:archive.unreferenced(state,current,path)
        except ValueError:continue
        selected.append(path.name)
        if len(selected)==2:break
    assert len(selected)==2
plan=archive.prepare(STATE,W/'publication-archive-01',selected)
p.save(W/'publication-archive-prepared.json',plan)
receipt=archive.apply(plan['plan'],plan['sha256'])
p.save(W/'publication-archive-applied.json',receipt)
assert len(list(pool.iterdir()))==30
print({'status':'ARCHIVED_TWO_RETIRED_GENERATIONS','selected':selected,'new_gpu_starts':0,'packages_deleted':False})
