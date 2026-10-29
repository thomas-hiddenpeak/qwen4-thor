"""Bounded rollback-only checks; no server, HTTP, model, or GPU invocation."""
import importlib.util
import json
from pathlib import Path
import tempfile

ROOT=Path('/home/rm01/models/dev/qwen4-thor')
BASE=ROOT/'.q4t-work/mtp-prefill-release-20261029'
spec=importlib.util.spec_from_file_location('release_cpu_delivery',ROOT/'tools/mtp_prefill_release/deliver.py')
d=importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
results=[]
with tempfile.TemporaryDirectory(prefix='delivery-cpu-',dir=BASE) as temp:
 root=Path(temp)
 package=root/'package'
 package.mkdir()
 (root/'build').mkdir()
 for arm in ['previous','candidate']:
  dest=package/arm
  dest.mkdir()
  (dest/'q4t').write_bytes(arm.encode())
  (dest/'CMakeCache.txt').write_text('test cache '+arm)
  (dest/'q4t.release.json').write_text(json.dumps(dict(binary_sha256=d.h.sha(dest/'q4t'),build_cache=str(dest/'CMakeCache.txt'),build_cache_sha256=d.h.sha(dest/'CMakeCache.txt'))))
 d.ROOT=root
 d.PACKAGE=package
 d.MARKER=root/'build/marker.json'
 d.OLD=d.h.sha(package/'previous/q4t')
 d.h.seal(package)
 for binary_arm,sidecar_arm in [('previous','previous'),('previous','candidate'),('candidate','previous'),('candidate','candidate')]:
  for name,arm in [('q4t',binary_arm),('q4t.release.json',sidecar_arm)]:
   (root/'build'/name).write_bytes((package/arm/name).read_bytes())
  d.MARKER.write_text(json.dumps(dict(package=str(package))))
  d.restore()
  assert not d.MARKER.exists()
  assert all(d.h.sha(root/'build'/name)==d.h.sha(package/'previous'/name) for name in ['q4t','q4t.release.json'])
  results.append(dict(case='restore-'+binary_arm+'-'+sidecar_arm,passed=True))
 (root/'build/q4t').write_bytes(b'foreign')
 try:
  d.restore()
 except RuntimeError:
  pass
 else:
  raise AssertionError('unknown binary accepted')
 assert (root/'build/q4t').read_bytes()==b'foreign'
 results.append(dict(case='unknown-binary-preserved',passed=True))
 (root/'build/q4t').write_bytes((package/'candidate/q4t').read_bytes())
 d.MARKER.write_text(json.dumps(dict(package='foreign')))
 try:
  d.restore()
 except RuntimeError:
  pass
 else:
  raise AssertionError('foreign marker accepted')
 assert d.h.sha(root/'build/q4t')==d.h.sha(package/'candidate/q4t')
 results.append(dict(case='foreign-marker-preserved-before-mutation',passed=True))
receipt=dict(passed=True,cases=results,gpu_starts=0,http_requests=0,models_read=0,tool_sha256=d.h.sha(ROOT/'tools/mtp_prefill_release/deliver.py'),test_sha256=d.h.sha(Path(__file__)))
(BASE/'delivery-cpu-check.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
