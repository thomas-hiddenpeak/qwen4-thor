"""Finish receipt indexing; default acceptance lives in its generation."""
import json
from pathlib import Path
from tools.serve_service.package import read, sha

WORK=Path('.q4t-work/serve-main-integration-20261112')
OUT=Path('docs/evidence/serve-cache-route-delivery-20261112')
result=read(WORK/'delivery-01/result.json')
current=result['publication']['current']
generation=Path('build/q4t').resolve().parent
identity=dict(publication_revision=94,engine_binary_sha256=sha('build/q4t'),
    engine_release_sha256=sha('build/q4t.release.json'),
    generation_sha256=current['generation_sha256'],manifest_sha256=current['manifest_sha256'],
    package=current['package'],service_acceptance_sha256=sha(generation/'service.acceptance.json'),
    service_release_sha256=sha('build/service.release.json'))
seconds=sum(read(WORK/(s+'.json'))['seconds'] for s in ('cache-profiles-business-01',
    'cache-profiles-business-02','cache-runtime-business-01','cache-runtime-business-02'))
review=dict(status='PASS_TARGETED_CACHE_ROUTES_DELIVERED',latest_release=identity,
    counts_including_failures=dict(model_starts=4,model_requests=15,strict_guard_commands_seconds=seconds),
    cache_business=read(WORK/'cache-business-02/acceptance.json'),
    profile_evidence='docs/evidence/serve-cache-config-contract-20261112/profiles-02/acceptance.json',
    delivery_commands=11,delivery_gpu_starts=0,state='STOPPED',
    rollback_manifest_sha256='32d3b81900e1339918dfed97663e441a6628a360137a5420da98c5b21eec5853',
    known_rollback_limit='rev92 predates these fixes',master_goal='active',mainline_merged=False)
(OUT/'review.json').write_text(json.dumps(review,indent=2)+'\n')
(OUT/'README.md').write_text(f'''# 缓存取消清理、可选路由和配置合同已可回退发行

2026-11-12。默认rev94，包{identity['manifest_sha256'][:12]}，引擎{identity['engine_binary_sha256'][:12]}。
11项实际STOPPED检查通过，仅替换产物和显式登记版本周期，无模型启动。
核心、客户端、outer状态保持；profile只更新资格绑定，容量/历史/预算保持。
旧版本注册完整保留，outer累计启动0次。

实际缓存业务1启动7请求102.806秒，97次NVML全OK；共享/私有输出和概率一致，
暂停已核验的自有worker后有界失效并同PID继续。普通缓存恢复9196token，
MTP恢复8570token。7请求全部成功、无重放或模型自动重启，进程/端口/锁清理。
主/轻2启动8请求资格复用。含失败累计4启动15请求，严格驱动{seconds:.3f}秒。

可选nginx66项路由/7项双入口CPU合同、schema的3实际响应/39负控随包交付。
默认仍为原生入口，不要求nginx/systemd。源变化限定缓存4文件、测试诊断头和
可选路由；原生helper字节及控制器保持。测试CMake变化单独说明，未伪称重编译。

原rev92包、generation和接受证据完整保留，但它包含修前取消清理及路由限制。
人工整根回退需要STOPPED和显式登记版本周期；未重跑未变恢复机制或长稳。
发行池满时原子归档1个不再被活动状态引用的旧副本，不删除包或证据。
首次私有目录775被拒，改700后正常归档；首次证据整理错找build根的acceptance，
修为真实generation路径，原发行与业务结果均未改写。

PR23仍修公共CI的Batch夹具和legacy沙箱，尚未合主线。总目标保持active，
本机发行不替代剩余A/R/Q任务完成。所有原件与索引保留，不复制缓存/凭据。
''')
(OUT/'finish_delivery_archive.py').write_bytes(Path(__file__).read_bytes())
(OUT/'SHA256SUMS').write_text(''.join(sha(f)+'  '+str(f.relative_to(OUT))+'\n'
    for f in sorted(OUT.rglob('*')) if f.is_file() and f.name!='SHA256SUMS'))

path=Path('docs/STATUS.md');text=path.read_text()
for label,key,previous in (
    ('发布','manifest_sha256','32d3b81900e1339918dfed97663e441a6628a360137a5420da98c5b21eec5853'),
    ('Generation','generation_sha256','56afa126d9406a55908c985a966ebf52d8f16c6dea6eb6e72d6fb46c1505c35f'),
    ('引擎','engine_binary_sha256','a06155e93ed84f2d2fb9ad8604ef91e644eccf74077b9b4d1912f906a7679169'),
    ('引擎发布记录','engine_release_sha256','e1ba8ff4a28048ccb43a2d0c8119f228987f9f341a295847e5c711ba8af48621')):
    old=next(l for l in text.splitlines() if l.startswith('| '+label+' |'))
    new=old.replace(previous,identity[key]).replace('rev92','rev94')
    text=text.replace(old,new,1)
old=next(l for l in text.splitlines() if l.startswith('| 版本回退 |'))
text=text.replace(old,'| 版本回退 | 原rev92包 `'+review['rollback_manifest_sha256']+'`；保留修前已知限制 |',1)
a=text.index('[发行结果与回退记录]');b=text.index('PR #22',a)
text=text[:a]+'''[发行结果与回退记录](evidence/serve-cache-route-delivery-20261112/README.md)
包含11项STOPPED交付检查，0新增GPU；真实缓存业务与原资格组合接受。
旧rev92保留为回退，核心、Key/配额和outer状态保持；profile只更新
资格绑定，版本周期显式登记并完整保留前驱。回退含修前已知缺陷，
未重跑未变恢复机制或宣称长稳。总目标仍active。
'''+text[b:]
text=text.replace('## 最近完成的实现和验证\n','''## 最近完成的实现和验证

- R01取消清理、N05可选路由和配置schema已[可回退发行rev94](evidence/serve-cache-route-delivery-20261112/README.md)。
  缓存7请求同PID成功并恢复目标/MTP布局，含失败累计4启动15请求。
  11项STOPPED交付保持恢复预算。PR23仍修实际CI的Batch/legacy沙箱，
  尚未合主线；继续全部master任务。
''',1)
text=text.replace('N05可选前置新增路由已验，封包/主线交付待完成',
                  'N05可选前置路由已随rev94发行，主线待完成')
text=text.replace('R01取消后缓存清理遗漏已实修并过CPU，业务/交付待完成',
                  'R01取消后缓存清理修复已过CPU/真实业务并随rev94发行')
path.write_text(text)
path=Path('docs/log/2026-11-12.md');text=path.read_text();i=text.index('\n\n')+2
path.write_text(text[:i]+'''## 缓存取消清理与可选入口修复完成rev94交付

- 日期：2026-11-12。缓存7请求同PID继续并恢复普通/MTP缓存；主/轻资格
  复用，含失败累计4启动15请求。11项STOPPED交付通过，旧rev92保留，
  恢复预算和旧周期不重置，未变控制器/TLS/检索直接复用证据。
- 为什么：可选入口和配置声明必须实际进包；退役副本原子归档1项，
  不删除证据。私有目录权限拒绝和证据整理路径错误保留并已纠正。
- 下一步：修复PR23公共CI的Batch夹具/legacy沙箱，通过后真实合主线，
  继续全部master；长稳由用户执行，不把阶段交付标成总目标完成。

'''+text[i:])
path=Path('docs/serve-master-goals-v2.json');value=read(path)
for row in value['tasks']:
    if row['id'] in ('R01','N05'):
        row['state']='delivered_pending_mainline'
        row['release']=dict(manifest_sha256=identity['manifest_sha256'],
            release_sha256=identity['service_release_sha256'],engine_sha256=identity['engine_binary_sha256'],
            engine_release_sha256=identity['engine_release_sha256'],
            rollback_sha256=review['rollback_manifest_sha256'],state='STOPPED',
            merged_main=False,whole_task_complete=False)
        row['evidence'].append('evidence/serve-cache-route-delivery-20261112/README.md')
        row['remaining']=['Complete actual PR23 mainline integration after remaining public host failures are repaired']
    if row['id']=='R01':
        row['findings'][-1]['state']='implemented_targeted_cpu_business_verified_delivered'
        row['findings'][-1]['delivery_evidence']='evidence/serve-cache-route-delivery-20261112/README.md'
        row['cancel_cleanup_business_bounds']=dict(model_starts=4,requests=16,seconds=2100,
            actual_starts=4,actual_requests=15)
    if row['id']=='N05':
        row['development_status']='Native transport and current optional frontend route parity delivered in rev94; mainline pending.'
        row['optional_route_parity'].update(state='implemented_verified_delivered',default_changed=True,
                                           delivery_revision=94)
    if row['id']=='Q02':
        row['latest_release']=identity
        row['evidence'].append('evidence/serve-cache-route-delivery-20261112/README.md')
        row['main_integration']['remaining_failures']=[
            'Legacy document sandbox prerequisites on hosted runner',
            'Batch fault-injection fixture short I/O timeout also applied to unrelated setup/verification']
path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(identity));print('strict command seconds',seconds)
