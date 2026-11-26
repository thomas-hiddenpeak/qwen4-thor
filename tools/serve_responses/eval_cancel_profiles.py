"""Qualify historical SDK cancellation across managed capacities and recovery."""
import json
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = 'eval_cancel_handoff'
RECEIPT = 'eval-cancel-handoff.json'
ACTOR = Path(__file__).with_name('eval_cancel_sdk.py')
PROOF = lifecycle.ROOT / 'docs/evidence/serve-sdk-routes-20261126/business.json'
RELEASE = lifecycle.ROOT / 'docs/evidence/serve-visual-frame-parts-20261126/delivery.json'


def prepare(out, build, deployment, interpreters):
    proof = package.read(PROOF)
    package.require(proof['stage'] == 'native_tls_sdk_verified' and
                    proof['old']['status'] == proof['current']['status'] == 'canceled',
                    'missing standalone Evals cancellation observation')
    sdk = {}
    for version in ('2.8.1', '3.55.1'):
        # Keep the venv executable path; resolve() would select the system interpreter.
        executable = Path(interpreters[version]).absolute()
        observed = subprocess.check_output(
            [executable, '-I', '-B', '-c', 'import openai; print(openai.__version__)'],
            text=True, timeout=15).strip()
        package.require(observed == version, 'unexpected OpenAI SDK interpreter version')
        sdk[version] = str(executable)
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.FRAME_REVIEWED,
                                KEY, [ACTOR, PROOF, RELEASE, Path(__file__)])
    for name in ('primary', 'light'):
        path = out / name / 'plan.json'
        plan = package.read(path)
        plan[KEY]['interpreters'] = sdk
        package.save(path, plan)


def check(row, previous=None):
    old, current = row['old'], row['current']
    eid, rid = old['eval_id'], old['run_id']
    base = f'/v1/evals/{eid}/runs/{rid}'
    package.require(re.fullmatch(r'eval_[0-9a-f]{32}', eid) and
                    re.fullmatch(r'evalrun_[0-9a-f]{32}', rid) and
                    old['sdk'] == '2.8.1' and current['sdk'] == '3.55.1' and
                    old['http'] == current['http'] == 200 and
                    old['status'] == current['before'] == current['status'] == 'canceled' and
                    old['resource_sha256'] == current['resource_sha256'] and
                    re.fullmatch(r'[0-9a-f]{64}', old['resource_sha256']) and
                    old['before'] in ('queued', 'in_progress') and old['malformed'] == 400 and
                    old['foreign_cancel'] == old['peer'] == current['peer'] == 404 and
                    old['path'] == base and current['path'] == base + '/cancel' and
                    current['eval_id'] == eid and current['run_id'] == rid and
                    current['fresh_chat'] == '42' and 'deleted' not in current,
                    'wrong project, run, cancellation path or terminal state')
    if previous is None:
        package.require(row['restored'] is None, 'unexpected previous evaluation')
        return
    check(previous)
    original = previous['current']
    restored = row['restored']
    restored_fields = {k: v for k, v in original.items() if k != 'fresh_chat'}
    package.require(eid != original['eval_id'] and rid != original['run_id'] and
                    restored == dict(restored_fields, deleted=404),
                    'original Eval Run, cancellation, ownership or cleanup changed')


def exercise(plan, interpreters, errors, previous=None, *, captures=None):
    if captures is not None:
        captures.mkdir(mode=0o700)

    def call(version, params, label):
        if captures is not None:
            params = dict(params, wire_output=str(captures / (label + '.json')))
        proc = subprocess.run([interpreters[version], '-I', '-B', ACTOR],
                              input=json.dumps(params), text=True, stdout=subprocess.PIPE,
                              stderr=errors, timeout=75, check=True)
        result = json.loads(proc.stdout)
        package.require(result['sdk'] == version, 'client version changed during qualification')
        return result

    def review(old, cleanup):
        return call('3.55.1', dict(plan, eval_id=old['eval_id'], run_id=old['run_id'],
                                   delete_after=cleanup, verify_chat=not cleanup),
                    'restored' if cleanup else 'canonical')

    restored = review(previous['old'], True) if previous is not None else None
    old = call('2.8.1', plan, 'legacy')
    current = review(old, False)
    row = dict(old=old, current=current, restored=restored)
    check(row, previous)
    return row
