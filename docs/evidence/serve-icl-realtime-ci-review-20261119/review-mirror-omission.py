import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path.cwd()
OUT = ROOT / '.q4t-work/serve-realtime-20261119/inventory-ci-fix-01'
PUBLIC = ROOT / 'docs/evidence/serve-icl-realtime-ci-review-20261119'
HEAD = '0b0321471e2f4ef9fc4f74cae38a99fe3bb64c8d'
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == HEAD

def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')

reviews = {
    'tools/serve_recovery/bundle.py':
        'New native packages bind all 74 build inputs, including 12 WebSocket/duplex/crypto inputs. Existing package byte identities remain immutable; verified actual 63 compiler dependencies and 24 omission/drift controls.',
    'tools/serve_recovery/profile_acceptance.py':
        'Explicit ICL and actual Realtime capability-to-checklist equality; bounded two profiles/entries/sessions, actual ports and SDK identities; only exact completed primary voice evidence may be reused, with original failed process and cumulative starts/requests/time retained.',
    'tools/serve_recovery/profile_evidence.py':
        'Reference geometry, codebook, consent ownership, codec SHA and full reference token usage required. Realtime requires three genuine turns, actual 70/81/70 input, waveform hash, deletion/history and all resource settlement. Old qualification cannot acquire new capability by relabeling.',
    'tools/serve_recovery/profile_realtime.py':
        'Frozen actual clip, TLS credentials, pinned complete SDK modules, one explicit WebSocket connection per profile; no automatic replay. Exact processed waveform shared only after byte equality; ingress cleanup always in finally. Fixed budget and actual counters retained.',
    'tools/serve_recovery/profile_voice_review.py':
        'Read-only reuse exclusively for the named old health-assertion failure: exact binary/config/assets/requests/wires and complete checklist, strict original NVML/cleanup and failed-status retention; other failures or identity changes rejected. No inference or network in review.',
    'tools/serve_service/package.py':
        'Full registered ICL declaration and reference_encoder/base-reference identity bound together. Historical base-speaker declarations keep their old checks; omitted or changed conditioning cannot pass as reference ICL.'
}
evidence = [
 'docs/evidence/serve-realtime-native-inventory-20261119/README.md',
 'docs/evidence/serve-icl-profile-health-review-20261119/README.md',
 'docs/evidence/serve-icl-package-health-20261119/README.md',
 'docs/evidence/serve-icl-realtime-primary-light-20261119/README.md',
 'docs/evidence/serve-icl-realtime-managed-recovery-20261119/README.md',
 'docs/evidence/serve-icl-realtime-delivery-20261119/README.md']
assert all((ROOT/p).is_file() for p in evidence)
path = ROOT/'tools/serve_audit/inventory.json'
assert path.stat().st_nlink == 1
old = json.loads(path.read_text())
source = {p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in reviews}
save(OUT/'plan.json', dict(head=HEAD, reviews=reviews, sources=source,
    evidence_reused=evidence, cpu_commands=1, seconds=60, gpu_actions=0,
    draft_semantic_sources_excluded=True, runtime_changed=False))

# Read-only views of tracked source paths exclude the two next-stage drafts.
# Do not move, stash, alter or temporarily hide the user's working files.
view = OUT/'tracked-view'
tracked = subprocess.check_output(['git','ls-files','-z']).decode().split('\0')
branches = {'', 'src', 'src/server', 'include', 'include/q4t',
            'include/q4t/server'}
def mirror(prefix):
    (view/prefix).mkdir()
    stem = prefix+'/' if prefix else ''
    children = {p[len(stem):].split('/')[0] for p in tracked
                if p and p.startswith(stem)}
    for child in children:
        name = stem+child
        if name in branches:
            mirror(name)
        else:
            (view/name).symlink_to(ROOT/name)
mirror('')
sys.path.insert(0, str(ROOT))
from tools.serve_audit import check_inventory as check
check.ROOT = view
started = time.monotonic()
actual = check.current_surface(view)
changed = sorted(k for k in old['sources_sha256'].keys() | actual['sources_sha256'].keys()
                 if old['sources_sha256'].get(k) != actual['sources_sha256'].get(k))
assert changed == sorted(reviews), changed
save(OUT/'original-review.json', dict(errors=check.check(old, actual, view), changed=changed))
old['sources_sha256'].update(source)
old['icl_realtime_delivery_review_20261119'] = dict(
    reviewed_head=HEAD, source_review=reviews, evidence=evidence,
    result='source identities reconciled with qualified native business and recovery',
    inventory_does_not_certify_all_semantics=True, default_revision=336,
    no_new_gpu_work=True, master_complete=False)
save(path, old)
errors = check.check(old, actual, view)
mutations = check.mutation_checks(old, actual)
result = dict(status='PASS' if not errors else 'FAIL', errors=errors,
    seconds=time.monotonic()-started, mutation_checks=mutations,
    reviewed_source_anchors=len(actual['sources_sha256']),
    fields=len(actual['fields']), completion_fields=len(actual['completion_fields']),
    routes=len(actual['routes']), native_routes=len(actual['native_routes']),
    source_changes=source, runtime_changed=False, gpu_actions=0,
    engine_evidence_reused=True, full_semantics_certified=False)
save(OUT/'result.json', result)
assert not errors and result['seconds'] < 60, result
PUBLIC.mkdir()
for name in ('plan.json','original-review.json','original-ci-failure.log','result.json','review.py',
             'view-preparation-failure.json','review-enospc.py'):
    (PUBLIC/name).write_bytes((OUT/name).read_bytes())
print(json.dumps(result, ensure_ascii=False))
