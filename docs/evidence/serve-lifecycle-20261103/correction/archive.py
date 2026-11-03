"""Lossless evidence archive: original failure plus accepted targeted correction."""
import gzip,hashlib,json,shutil,sys,tarfile
from pathlib import Path
ROOT=next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists());sys.path.insert(0,str(ROOT))
from tools.serve_lifecycle import repair as r,stage
h=r.h;destination=ROOT/'docs/evidence/serve-lifecycle-20261103'
h.require(not destination.exists(),'preserve archive')
summary=h.read(r.BASE/'acceptance-summary.json')
h.require(summary['status']=='ACCEPT_BOUNDED_LIFECYCLE_WITH_TARGETED_SHUTDOWN_REPAIR','acceptance gate')
paths=[]
for prefix,base in [('original',stage.BASE),('correction',r.BASE)]:
 for label in (('package','execution','rollback-bundle') if prefix=='correction' else ('package','execution')):
  h.verify_seal(base/label)
  paths.extend((prefix+'/'+str(p.relative_to(base)),p) for p in (base/label).rglob('*') if p.is_file())
for name in ('summarize.py','archive.py','custody.py','postflight.py','postflight.json','postflight-gpu.json',
             'release-history.json','acceptance-summary.json','offline-schema.json','original-evidence.json','rollback-readiness.json'):
 paths.append(('correction/'+name,r.BASE/name))
destination.mkdir();sources,local,readable={},{},[]
critical={'plan.json','reuse.json','runtime-reuse.json','release-history.json','manifest.json','begin.json',
 'finalized.json','exit.json','result.json','soak-result.json','resource-envelope.json','shutdown.json',
 'child-exit-observations.json','binding.json','q4t.release.json','shutdown-abort-accounting.json'}
with (destination/'raw-records.tar.gz').open('xb') as file:
 with gzip.GzipFile(filename='',mode='wb',fileobj=file,mtime=0) as gz:
  with tarfile.open(fileobj=gz,mode='w|',format=tarfile.PAX_FORMAT) as packed:
   for name,path in sorted(paths):
    h.require_regular(path);record=h.identity(path)
    with path.open('rb') as stream:magic=stream.read(4)
    if magic==b'\x7fELF' or path.suffix in ('.o','.a','.bundle'):local[name]=record;continue
    member=tarfile.TarInfo(name);member.size=record['bytes'];member.mode=0o600
    with path.open('rb') as stream:packed.addfile(member,stream)
    h.check_identity(record);sources[name]=record
    if (name.count('/')==1 or path.name in critical or '/diagnostics/' in name or
        '/switch-' in name or '/graceful-inflight/' in name and path.name.endswith(('-error.json','-wire.bin'))):
     target=destination/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
     h.require(h.sha(target)==record['sha256'],'raw copy differs');readable.append(name)
for name in ('q4t','q4t.release.json'):local['installed/'+name]=h.identity(ROOT/'build'/name)
verified=set()
with tarfile.open(destination/'raw-records.tar.gz',mode='r|gz') as packed:
 for member in packed:
  h.require(member.isfile() and member.name in sources and member.name not in verified,'raw member')
  stream=packed.extractfile(member);digest=hashlib.sha256();count=0
  while raw:=stream.read(1024**2):digest.update(raw);count+=len(raw)
  h.require(digest.hexdigest()==sources[member.name]['sha256'] and count==sources[member.name]['bytes'],'unpacked identity')
  verified.add(member.name)
h.require(verified==set(sources),'raw set differs')
h.save(destination/'source-map.json',sources);h.save(destination/'local-identities.json',local)
h.save(destination/'compression-proof.json',dict(passed=True,raw_members=len(sources),
 raw_bytes=sum(v['bytes'] for v in sources.values()),archive=h.identity(destination/'raw-records.tar.gz'),
 decompressed_each_member_sha256_verified=True,readable_duplicate_members=readable,
 original_records_unchanged=True,raw_logs_are_not_redacted=True))
(destination/'README.md').write_text(f'''# SRV-13 生命周期与停机修复证据

原冻结05d89984：旧3cd3994d完成同PID一小时240周期，但随后排队请求
停机分类错误，整体FAILED/guard COMMAND_FAILED。原包和执行记录原样保留。
修复冻结279c9abb：c597adc3只修停止准入，定向停机、崩溃、重启、实际
v3回退及安装C的真实业务通过；原总预算保持。旧ELF小时段明确复用，
不冒称C已经连续运行一小时。不作数值或性能优化接受声明。

`raw-records.tar.gz`无损保留{len(sources)}份原记录；每个解压成员都与
`source-map.json`的原路径/SHA/字节数核对。关键receipt有原字节可读副本，
完整wire、连续采样和日志在归档中。{len(local)}个ELF/bundle/安装文件留在
本机并登记身份；原package/execution seal的完整复验仍依赖这些本机文件。
根seal只核验Git归档，不冒充完整二进制包。没有修改原始失败记录。

原诊断私有目录的0700/0600在本机验证；Git只保证字节。日志/wire含
已结束实例的合成请求ID和取消凭据，归档不称已脱敏。未安装常驻监控、
生产supervisor或访问网关；不证明天级长稳或物理RAM硬限额。
详见[结果](../../SERVE_LIFECYCLE_RESULT_2026-11-03.md)。
''')
h.seal(destination)
files=[p for p in destination.rglob('*') if p.is_file()]
result=dict(raw_records=len(sources),raw_bytes=sum(v['bytes'] for v in sources.values()),
 archive_files=len(files),archive_bytes=sum(p.stat().st_size for p in files),
 readable_duplicates=len(readable),local_artifacts=len(local),root_seal=h.identity(destination/'sealed-files.json'))
h.save(r.BASE/'archive-result.json',result);print(json.dumps(result,indent=2))
