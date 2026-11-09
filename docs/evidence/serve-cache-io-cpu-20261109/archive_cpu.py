import hashlib,json,re,shutil,stat,subprocess
from pathlib import Path
ROOT=Path.cwd()
BASE=ROOT/'.q4t-work/serve-cache-io-20261109'
OUT=ROOT/'docs/evidence/serve-cache-io-cpu-20261109'
BUILD=ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,v):
 p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
def copy(a,b):
 assert stat.S_ISREG(a.lstat().st_mode),a
 b.parent.mkdir(parents=True,exist_ok=True)
 shutil.copyfile(a,b)
 assert sha(a)==sha(b)
labels=['build-01','configure-01','build-02','build-03','build-04','build-05','primitive-01','integration-01','session-01','prefix-01','incremental-01','target-01','session-02','prefix-02','target-02','health-01','inventory-01','inventory-02','declarations-01','declarations-02']
assert not OUT.exists()
OUT.mkdir(parents=True)
summary={}
for label in labels:
 result=json.loads((BASE/label/'result.json').read_text())
 assert result['exit_code']==(2 if label=='build-02' else 0),(label,result)
 if label.startswith('build-'):
  assert not re.search(r'\bwarning:',(BASE/label/'stderr.log').read_text())
 summary[label]=result
 for name in ('command.json','stdout.log','stderr.log','result.json'):
  copy(BASE/label/name,OUT/label/name)
for name in ('primitive-required.txt','integration-required.txt','target-required.txt','recorder-setup-failure.json','archive_cpu.py'):
 copy(BASE/name,OUT/name)
protocol=json.loads((BUILD/'generated/q4t/server/chat_protocol_sources.json').read_text())
assert all(sha(ROOT/n)==h for n,h in protocol.items())
flags=BUILD/'CMakeFiles/q4t_server.dir/flags.make'
assert 'Q4T_GRAMMAR_PROCESS_TESTS' not in flags.read_text()
for name,source in {'protocol-sources.json':BUILD/'generated/q4t/server/chat_protocol_sources.json','production-flags.make':flags}.items():
 copy(source,OUT/name)
candidate=BASE/'candidate'
candidate.mkdir()
for name in ('q4t','CMakeCache.txt'):
 copy(BUILD/name,candidate/name)
 (candidate/name).chmod(0o500 if name=='q4t' else 0o400)
changed=subprocess.check_output(['git','diff','--name-only'],text=True).splitlines()+subprocess.check_output(['git','ls-files','--others','--exclude-standard'],text=True).splitlines()
changed=sorted({n for n in changed if n.startswith(('src/','include/','tests/','tools/','cmake/')) or n=='CMakeLists.txt'})
for name in changed:copy(ROOT/name,candidate/'sources'/name)
(candidate/'source-diff.patch').write_bytes(subprocess.check_output(['git','diff','--',*changed]))
expected={
 'build/q4t':'92e600fbcdcadf37271a77405daf498f1fe4fbf2d327ad79a0bf3646bd02ce31',
 'build/q4t.release.json':'dfffcbadb6ba73c2184c06444f28d85979d2d0881d34aca7087bf1ff5fd31e75',
 'build/service.release.json':'e4d8b32171335bc535021b77712d6eb8c7f3e0e45b4498533afb4d5404eda05e',
 'build/service/manifest.json':'ffbd9d4b52175dc07356933fbfca54ad5143522d3ec9d6ca7bcedcacf0e8887e'}
assert all(sha(ROOT/n)==h for n,h in expected.items())
identity=dict(candidate=str(candidate/'q4t'),candidate_sha256=sha(candidate/'q4t'),
 build_cache_sha256=sha(candidate/'CMakeCache.txt'),protocol_source_files=len(protocol),
 source_base_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
 source_modified={n:sha(ROOT/n) for n in changed},protected_default=expected)
save(OUT/'identity.json',identity)
save(OUT/'decision.json',dict(status='PASS_TARGETED_CACHE_IO_CPU_PENDING_REAL_BUSINESS',
 distinct_cpp_cases=16,storage_programs=dict(private_checks=49,shared_checks=39,incremental_base_checks=55,incremental_checks=137),
 health_schema=dict(actual_cpp_fragments=2,negative_cases=9),source_anchors=301,inventory_mutations=5,
 gpu_starts=0,chats=0,default_replaced=False,full_R01_R03_complete=False,
 builds_and_checks=summary,
 preserved_failures={'recorder_setup':'NOT_RUN before command launch; missing output parent','build-02':'incremental fixture retained two old parent-only fault references; actual production engine and other targets built, fixture compile failed'},
 evidence_reuse=['primitive-01 original8; changed relative directory and old-generation close branches retested in integration-01',
 'integration-01:4 new cache cases,2 affected primitive cases,2 grammar domains; target-01:2 target layout cases',
 'flat allocation accounting added after integration-01: session-02, prefix-02, affected target-02 passed; unchanged incremental path reused incremental-01',
 'build-05 only wrapped two long source lines after build-04; no runtime test repeated for formatting',
 'original R03 recovery/observer-loss business preserved; cache/resource real business remains pending']))
(OUT/'README.md').write_text('''# R01缓存文件I/O隔离：针对性CPU证据\n\n原生worker独占文件，单控制器/域、单在途缓冲、单次IPC至多2秒；\n取消/无回执废弃代次，不重放，未回收不增殖，3次/分钟启动预算。\n会话/前缀、grammar/objects相互独立；正常业务不因可选存储失能停服。\n\n16个不同C++用例、私有/共享/增量真实字节程序及健康schema通过。\n普通快照追加实际磁盘块计量后，只重验私有、共享及受影响target项。\n原build-02夹具编译失败和记录器启动前NOT_RUN均保留；生产构建零警告。\nidentity.json绑定当前源与候选，decision.json逐项区分已执行及证据复用；\n这里不宣称所有历史用例都在最终ELF上重跑。\n\n本组0GPU/0Chat，默认保持92e600fb。下一步组合实际缓存/资源进度\n短验和可回退交付；R01/R03及全部Serve总计划未完成。\n''')
index={str(p.relative_to(OUT)):dict(sha256=sha(p),bytes=p.stat().st_size) for p in sorted(OUT.rglob('*')) if p.is_file()}
save(OUT/'index.json',dict(files=index))
print(json.dumps({'candidate_sha256':identity['candidate_sha256'],'indexed_files':len(index),'output':str(OUT)},indent=2))
