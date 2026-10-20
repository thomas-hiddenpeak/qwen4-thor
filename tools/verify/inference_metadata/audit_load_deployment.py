"""Offline audit of the accepted loader deployment and rollback package."""
import hashlib
import json
from pathlib import Path
root = Path(__file__).resolve().parents[3]
out = root / '.q4t-work/megatron-reliability-20261020/deploy-v1'
package = root / '.q4t-work/releases/megatron-reliability-20261020'
def read(p): return json.loads(p.read_text())
def sha(p):
    with p.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()
def require(ok, message):
    if not ok: raise ValueError(message)
manifest = read(out / 'sealed-files.json')
for name, digest in manifest.items():
    require(sha(out / name) == digest, name)
guard = read(out / 'lease/exit.json')
require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0, 'guard')
deploy = read(out / 'deployment.json')
require(deploy['status'] == 'DEPLOYED_HEALTH_CHECKED', 'deployment')
health = {}
for mode in ['on', 'off']:
    folder = out / ('health-' + mode)
    rows = read(folder / 'results.json')
    end = read(folder / 'exit.json')
    require(len(rows) == 1 and rows[0]['success'] == 1 and
            rows[0]['exact_match'] and rows[0]['length_match'] and
            rows[0]['actual_input'] == 1024, 'health ' + mode)
    require(end['server'] == 0 and end['http_output_checks_passed'], 'shutdown')
    health[mode] = {'requests': 1, 'exact_match': True, 'server_exit': 0}
require('MTP loaded (k=3' in (out / 'health-on/server.log').read_text(), 'default k')
for name, digest in read(package / 'manifest.json').items():
    require(sha(package / name) == digest, 'package ' + name)
meta = read(root / 'build/q4t.release.json')
require(sha(root / 'build/q4t') == deploy['binary_sha256'] == meta['binary_sha256'], 'binary')
require(sha(root / 'build/q4t.release.json') == sha(package / 'q4t.release.json'), 'sidecar')
require(sha(Path(meta['build_cache'])) == meta['build_cache_sha256'], 'cache')
require(sha(Path(meta['acceptance_snapshot'])) == meta['acceptance_snapshot_sha256'], 'decision')
for name, digest in read(out / 'rollback-identity.json').items():
    require(sha(out / 'previous' / name) == digest, 'backup ' + name)
old = read(out / 'previous/q4t.release.json')
require(sha(out / 'previous/q4t') == old['binary_sha256'] == deploy['previous_sha256'], 'old binary')
require(sha(Path(old['build_cache'])) == old['build_cache_sha256'] == sha(out / 'previous/CMakeCache.txt'), 'old cache')
seal = read(out / 'seal.json')
print(json.dumps({'status': 'PASS', 'sealed_files': len(manifest),
    'binary_sha256': meta['binary_sha256'], 'previous_sha256': old['binary_sha256'],
    'health': health, 'guard_status': guard['status'],
    'seconds': seal['finished'] - seal['started'], 'rollback_verified': True,
    'rollback_exercised': False, 'performance_samples': 0}, indent=2))
