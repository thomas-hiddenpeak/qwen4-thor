"""Reuse complete business evidence after the local /healthz client rejection.

No model/HTTP retry. Preserve original FAILED/COMMAND_FAILED receipts and the
missing /healthz observation; use actual ready/live/metrics plus owned exit.
"""
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_handoff import stage
from tools.serve_service import acceptance as client

h, a = stage.h, stage.a
FIRST = stage.OUTPUT
OUTPUT = stage.BASE/'correction'
PLAN = stage.BASE/'correction-plan.json'


def audit():
    plan = stage.check()
    h.verify_seal(FIRST)
    result = h.read(FIRST/'business/result.json')
    h.require(result['status'] == 'FAILED' and result['cleanup_confirmed'] and
        result['error'] == "InvalidObservation('unsupported_diagnostic_route')" and
        result['counts'] == dict(generations=9, negatives=1, controls=7, starts=1),
        'different or incomplete failure')
    guard = h.read(FIRST/'lease/exit.json')
    h.require(guard['status'] == 'COMMAND_FAILED' and guard['child_returncode'] == 1,
              'original guard has a different failure')
    h.require(h.read(FIRST/'native/result.json')['passed'], 'native lifecycle missing')
    folder = FIRST/'business'
    labels = ('ordinary-json', 'ordinary-sse', 'strict-before-tool', 'project-read',
              'project-answer', 'after-error', 'strict-sse', 'after-sse')
    ids = []
    for label in labels:
        request, http = h.read(folder/(label+'-request.json')), h.read(folder/(label+'-http.json'))
        raw = (folder/(label+'-wire.bin')).read_bytes()
        h.require(http['status'] == 200 and http['eof'], 'incomplete success wire')
        parsed = (client.read_sse if request['stream'] else client.read_json)(io.BytesIO(raw))
        h.require(parsed.id == request['request_id'] and parsed.model == request['model'], 'wire identity')
        ids.append(parsed.id)
        if label.startswith('ordinary'):
            h.require(parsed.message['content'].strip() == '710003', 'ordinary answer')
        elif label in ('strict-before-tool', 'after-error', 'strict-sse', 'after-sse'):
            h.require(json.loads(parsed.message['content']) == {'ok': True}, 'strict answer')
        elif label == 'project-read':
            calls = client.prepare_tool_calls(parsed, client.VALIDATORS)
            h.require(len(calls) == 1 and calls[0].arguments == dict(
                path='docs/MODEL.md', start_line=1, line_count=24), 'tool arguments')
        else:
            tool = json.loads(h.read(folder/'project-tool-result.json')['content'])
            client.validate_answer(parsed.message['content'], {tool['source']['citation']}, 1)
            h.require('Qwen4ExpForConditionalGeneration' in parsed.message['content'], 'project answer')
    error_http = h.read(folder/'incomplete-http.json')
    h.require(error_http['status'] == 502 and error_http['eof'] and
        json.loads((folder/'incomplete-wire.bin').read_bytes())['error']['code'] ==
        'structured_output_incomplete' and len(set(ids)) == 8, 'negative wire/duplicate request')
    metrics = h.read(folder/'final-metrics.json')
    h.require(all(metrics[k] == 0 for k in client.EMPTY) and
        metrics['q4t_seq_slots_free'] == metrics['q4t_seq_slots_total'] == 1 and
        metrics['q4t_requests_total'] == 9 and metrics['q4t_requests_success_total'] == 8 and
        metrics['q4t_requests_error_total'] == 1 and metrics['q4t_requests_aborted_total'] == 0 and
        metrics['q4t_structured_rejected_total'] == 0 and metrics['q4t_gpu_healthy'] == 1 and
        metrics['q4t_structured_workers_started_total'] == metrics['q4t_structured_workers_reaped_total'] == 7,
        'resource/outcome accounting')
    for path in ('livez', 'readyz'):
        receipt = h.read(folder/('final-'+path+'.json'))
        client.contracts.readiness(json.loads(receipt['body']), receipt['http_status'], liveness=path == 'livez')
    h.require(h.read(folder/'scope/payload-exit.json')['returncode'] == 0 and
        h.read(folder/'scope/cleanup.json')['unit_removed'] and
        'shutdown complete (0 in-flight remaining)' in (folder/'server.log').read_text(), 'owned shutdown')
    h.require(h.read(folder/'gpu-after.json')['pids'] == [] and
              h.read(folder/'gpu-after.json')['status'] == 'OK', 'original GPU cleanup')
    return plan, result


def verifier(plan, installed=False):
    if installed:
        return stage.verifier(plan, installed=True)
    _, prior = audit()
    shutil.copytree(FIRST/'business', OUTPUT/'business')
    (OUTPUT/'business/result.json').rename(OUTPUT/'business/original-result.json')
    h.save(OUTPUT/'business/result.json', dict(status='PASS_BOUNDED_STRUCTURED_HANDOFF',
        original_result=h.identity(FIRST/'business/result.json'), corrected_driver_only=True,
        counts=prior['counts'], successful_requests=8, expected_errors=1,
        new_model_starts=0, new_generations=0, new_http_observations=0,
        missing_healthz=True, actual_health_evidence=['final-livez.json', 'final-readyz.json', 'final-metrics.json'],
        missing_final_standalone_sample=True, cleanup_source='owned scope fallback stop; returncode 0',
        cleanup_confirmed=True, automatic_request_retry=False, performance_acceptance=False))


def prepare():
    h.require(not PLAN.exists() and not OUTPUT.exists(), 'preserve correction')
    h.seal(FIRST)
    audit()
    h.save(PLAN, dict(source=h.identity(Path(__file__).resolve()),
        original_seal=h.identity(FIRST/'sealed-files.json'),
        original_started=h.read(FIRST/'begin.json')['started'], limit_seconds=stage.LIMITS['seconds'],
        new_model_starts=0, new_generations=0, new_http_observations=0))


def check():
    correction = h.read(PLAN)
    h.check_identity(correction['source']); h.check_identity(correction['original_seal'])
    plan, _ = audit()
    plan['started'] = plan['original_started'] = correction['original_started']
    h.require(time.time() < plan['started']+stage.LIMITS['seconds'], 'original phase deadline')
    return plan


def bind():
    stage.OUTPUT = OUTPUT
    stage.bind()
    a.check, a.verifier = check, verifier
    stage.transaction.check, stage.transaction.verifier = check, verifier


def execute():
    h.require_guard(OUTPUT)
    plan = check()
    h.save(OUTPUT/'begin.json', dict(started=plan['started'], limits=stage.LIMITS))
    bind()
    stage.transaction.execute_locked()


def finalize():
    check(); bind(); a.finalize(); h.seal(OUTPUT)


if __name__ == '__main__':
    action = sys.argv[1]
    if action == 'launch':
        check(); OUTPUT.mkdir()
        raise SystemExit(subprocess.call(['/usr/bin/python3', '-B',
            str(ROOT/'tools/evalscope/gpu_exclusive.py'), '--output', str(OUTPUT/'lease'), '--',
            '/usr/bin/python3', '-B', str(Path(__file__).resolve()), 'execute'], cwd=ROOT))
    if action not in ('prepare', 'execute', 'finalize'): raise SystemExit('unknown action')
    globals()[action]()
