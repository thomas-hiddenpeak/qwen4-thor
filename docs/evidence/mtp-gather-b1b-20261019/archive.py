"""Copy bounded review evidence; large/raw artifacts retain their original paths."""
import hashlib,json,shutil
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor'); source=Path(__file__).resolve().parent
target=root/'docs/evidence/mtp-gather-b1b-20261019'; target.mkdir(parents=True,exist_ok=True)
manifest=[]
for path in sorted(source.rglob('*')):
    if path.is_file():
        with path.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
        manifest.append({'path':str(path.relative_to(root)),'bytes':path.stat().st_size,'sha256':digest})
(target/'raw-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
# Environment is deliberately allowlisted for the public review bundle.
env=json.loads((source/'environment.json').read_text())
public={k:v for k,v in env.items() if k.startswith('Q4T_') or k in ['CUDA_HOME','LD_LIBRARY_PATH','LD_PRELOAD','CUDA_VISIBLE_DEVICES']}
(target/'environment.json').write_text(json.dumps(public,indent=2)+'\n')
for path in sorted(source.iterdir()):
    if path.is_file() and path.suffix in ['.py','.json','.log','.required','.patch','.md'] and not path.name.startswith('environment'):
        shutil.copy2(path,target/path.name)
for group in ['quality-on','candidate-on','quality-off','candidate-off','parent-off','parent-off-r1','parent-on','lease','lease-resume-01']:
    (target/group).mkdir(exist_ok=True)
    for name in ['results.json','exit.json','binary.sha256','run-mode.json','server-command.json','capacity.json','plan.json','command.json']:
        path=source/group/name
        if path.exists():shutil.copy2(path,target/group/name)
(target/'README.md').write_text('''# B1b有界交付证据\n\n原始证据保留 `.q4t-work/b1b-20261019/`，包括HTTP请求/响应、SQLite、\n父/候选与测试binary、12对原始状态文件、完整监控。raw-manifest逐文件\n绑定路径/大小/SHA；大文件不入git。此目录保存审查摘要、运行驱动、\n身份、构建/合同日志与HTTP结果。environment仅公开运行相关白名单。\n\n具体接受边界与风险见 [交付记录](../../MTP_GATHER_B1B_2026-10-19.md)。\n提交/推送不是验收通过或部署。\n''')
print('archived',len(manifest),'raw identities')
