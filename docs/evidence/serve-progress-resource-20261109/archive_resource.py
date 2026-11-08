import json
from pathlib import Path
import shutil
import stat
from tools.serve_service.package import read, sha

root = Path.cwd()
base = root / '.q4t-work/serve-progress-20261109'
build = root / '.q4t-work/serve-custom-tools-20261108/engine-build'
out = root / 'docs/evidence/serve-progress-resource-20261109'
out.mkdir(exist_ok=False)
shutil.copyfile(__file__, out / 'archive_resource.py')
for folder in ('build-07', 'resource-cpu-01', 'resource-python-01', 'inventory-04'):
    assert read(base / folder / 'result.json')['exit_code'] == 0
    for source in (base / folder).rglob('*'):
        if not stat.S_ISREG(source.lstat().st_mode):
            continue
        dest = out / source.relative_to(base)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
assert '4 tests, 4 passed, 0 failed, 0 skipped' in (base / 'resource-cpu-01/stdout.log').read_text()
assert 'Ran 3 tests' in (base / 'resource-python-01/stderr.log').read_text()
assert not any('warning:' in (base / 'build-07' / n).read_text().lower()
    for n in ('stdout.log', 'stderr.log'))
sources = ('include/q4t/server/request_diagnostics.h',
    'src/server/chat_generation.cpp', 'tests/server_progress_context_test.cpp',
    'tools/serve_recovery/progress.py', 'tools/serve_recovery/test_progress.py')
decision = dict(status='PASS_TARGETED_CACHE_RESOURCE_PROGRESS_CPU',
    different_tests=7, source_sha256={n: sha(root / n) for n in sources},
    candidate_sha256=sha(build / 'q4t'),
    host_tests_sha256=sha(build / 'q4t_host_tests'),
    gpu_starts=0, default_changed=False,
    reused_real_recovery='serve-progress-business-20261109',
    pending=['bounded optional cache filesystem isolation',
        'combined actual cache/resource business', 'rollback-capable delivery'])
(out / 'decision.json').write_text(json.dumps(decision, indent=2)+'\n')
(out / 'README.md').write_text('''# R03缓存与请求资源进度补齐

2026-11-09，7项针对CPU通过，实际q4t/host零警告构建，296来源与5
负控通过。候选ab5e66c3，默认92e600fb不变；无本组新增GPU。

同步GPU复制、请求分配/释放和初始/错误复位被标记为真实执行，
嵌套保持原工作ID，暂时退出缓存等待，结束恢复阶段/等待。异常
与scope析构不冒充完成，空指针释放不增加完成。实际成功分配/释放
和D2H才增加对应完成；pageable H2D沿用后续readback完成边界。

缓存/资源执行沿用work=4及120秒预算，仍要求请求期限30秒宽限和
2秒不同新鲜快照；文件等待不触发GPU执行超期。已过主循环恢复、
工具和观察断连业务原件保留复用；新缓存/资源真实组合仍待验证。

源码复核发现同步缓存文件I/O持模型锁，R01已重新登记为未解决。
接着原生隔离后统一针对性业务，不能以此CPU组宣布全部恢复完成。

CUDA语义依据：[13.3同步行为](https://docs.nvidia.com/cuda/archive/13.3.0/cuda-runtime-api/api-sync-behavior.html)。
没有新增CUDA同步、改变张量计算，未做完整性能或全模型测试。
''')
shutil.copyfile(base / 'resource-required.txt', out / 'resource-required.txt')
index = {str(p.relative_to(out)): dict(bytes=p.stat().st_size, sha256=sha(p))
    for p in sorted(out.rglob('*')) if p.is_file()}
(out / 'index.json').write_text(json.dumps(index, indent=2)+'\n')
print(json.dumps(dict(files=len(index), different_tests=7,
    candidate_sha256=decision['candidate_sha256'])))
