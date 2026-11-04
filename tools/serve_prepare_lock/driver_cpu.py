"""Offline rejection checks for incomplete or unsuccessful native evidence."""
import copy
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_prepare_lock import cpu,stage


def main():
    out=cpu.BASE/stage.DRIVER_LABEL;out.mkdir()
    paths=sorted((ROOT/'tools/serve_prepare_lock').glob('*.py'))
    cpu.save(out/'sources.json',{str(p.relative_to(ROOT)):cpu.sha(p) for p in paths})
    for path in paths:compile(path.read_bytes(),str(path),'exec')
    rows=[dict(mode=mode,cause=cause,passed=True,returned_before_owner_release=True,seconds=.01)
          for mode in ('encode-json','encode-sse','thinking-json','thinking-sse','scoped-sse','preflight')
          for cause in ('explicit','deadline','disconnect','shutdown')
          if (mode,cause)!=('preflight','explicit')]
    stage.verify_native(rows,0)
    bad=[('nonzero-exit',rows,1),('missing-case',rows[:-1],0),
         ('duplicate-case',rows[:-1]+rows[:1],0)]
    for key,value in (('passed',False),('returned_before_owner_release',False),
                      ('seconds',2),('seconds',float('nan'))):
        altered=copy.deepcopy(rows);altered[0][key]=value
        bad.append((key+':'+str(value),altered,0))
    rejected=[]
    for label,items,code in bad:
        try:stage.verify_native(items,code)
        except RuntimeError:rejected.append(label)
        else:raise RuntimeError('bad evidence accepted: '+label)
    cpu.save(out/'result.json',dict(passed=True,synthetic_gate_rejections=rejected,
        source_syntax_checked=len(paths),models=0,http=0,gpu=0,
        scope='Evidence gate only; actual handler/runtime acceptance remains pending'))


if __name__=='__main__':main()
