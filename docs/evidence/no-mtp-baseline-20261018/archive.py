"""Archive complete metadata and a raw-data inventory, without running inference."""
from pathlib import Path
import json,hashlib,shutil
ROOT=Path(__file__).resolve().parent
REPO=Path('/home/rm01/models/dev/qwen4-thor')
DEST=REPO/'docs/evidence/no-mtp-baseline-20261018'
DEST.mkdir(exist_ok=False)
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()
manifest=[]
for p in sorted(ROOT.rglob('*')):
    if not p.is_file() or '__pycache__' in p.parts:continue
    rel=p.relative_to(ROOT)
    manifest.append({'path':str(rel),'size':p.stat().st_size,'sha256':sha(p)})
    # Keep original raw inputs / databases / client reports at their paths.
    # Commit scripts, phase identity, exit records, server logs, results and responses.
    selected=len(rel.parts)==1 or (rel.parts[0]=='lease' and p.name=='exit.json') or (rel.parts[0] in ['Q','A0','A1'] and (len(rel.parts)==2 and p.name in ['binary.sha256','commit.txt','worktree.patch','CMakeCache.txt','evalscope-version.txt','server-command.json','server.log','server.pid','exit.json','events.jsonl','capacity.json','results.json','performance-plan.json'] or p.name in ['responses.json','request-boundaries.json','command.json','client-exit.json']))
    if selected:
        target=DEST/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,target)
(DEST/'raw-manifest.json').write_text(json.dumps({'raw_root':str(ROOT),'files':manifest},indent=2)+'\n')
(DEST/'README.md').write_text('# no-MTP 41请求基线证据\n\n完整原始证据位于`'+str(ROOT)+'`，逐文件大小/SHA见raw-manifest.json。\n本目录保留执行驱动、身份、退出状态、原始响应和汇总；DB/大输入及客户端完整报告留在原路径。\n性能是描述性基线，不是置信区间、分辨带或B1a接受证据。\n')
print(json.dumps({'raw_files':len(manifest),'archive':str(DEST)}))
