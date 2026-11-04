"""Verify original wires and lifecycle evidence; archive this agent's short run."""
from dataclasses import asdict
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_soak import stage, evidence
from tools.serve_service import package as pkg, acceptance as a
from tools.serve_handoff.stage import h

DEST = ROOT/'docs/evidence/serve-soak-short-20261103'


def check():
    plan = stage.check()
    result = pkg.read(stage.OUT/'result.json')
    pkg.require(result['status'] == 'PASS_CURRENT_MANAGED_'+stage.PROFILE.upper()+'_SOAK'
        and result['starts'] == 1 and result['cleanup_confirmed'] and
        result['successful_requests'] == stage.CYCLES*5 and
        result['counts']['generations'] == stage.CYCLES*5 and
        result['counts']['controls'] <= stage.CONTROL_LIMIT and
        result['gpu_window_elapsed'] < stage.GPU_MAX and
        result['elapsed_seconds'] < stage.TOTAL_MAX, 'run result incomplete')
    soak = pkg.read(stage.OUT/'soak.json')
    pkg.require(soak['cycles'] == stage.CYCLES and
        stage.SECONDS <= soak['seconds'] <= stage.SOAK_MAX and
        soak['day_scale_stability_proved'] == (stage.PROFILE == 'day'),
        'duration/profile evidence differs')
    capture = pkg.read(stage.OUT/'capture/capture.json')
    pkg.require(capture['passed'] and capture['resource_samples'] > 0,
                'continuous evidence incomplete')
    for name, row in capture['logs'].items():
        path = stage.OUT/'capture'/name
        pkg.require(path.stat().st_size == row['bytes'] and pkg.sha(path) == row['sha256'],
                    'captured stream identity differs')
    runtime = stage.OUT/'managed-run'
    terminal = pkg.read(runtime/'terminal.json')
    pkg.require(terminal['state'] == 'STOPPED' and terminal['cleanup_confirmed'] and
        terminal['worker']['attempts'] == 1 and terminal['worker']['restarts'] == 0,
        'service did not stop without replacement')
    guard = evidence.guard_archive(stage.OUT/'capture/events.jsonl',
                                   pkg.read(runtime/'lease/exit.json'))
    request_ids, cancel_tokens, quiescent = set(), set(), []
    for number in range(1, stage.CYCLES+1):
        folder = stage.OUT/f'cycle-{number:04d}'
        for label in ('plain', 'stream', 'strict', 'project-read', 'project-answer'):
            body = pkg.read(folder/(label+'-request.json'))
            http = pkg.read(folder/(label+'-http.json'))
            pkg.require(http['status'] == 200 and http['eof'], 'incomplete wire')
            pkg.require(body['request_id'] not in request_ids and
                        body['cancel_token'] not in cancel_tokens, 'credential reuse')
            request_ids.add(body['request_id']);cancel_tokens.add(body['cancel_token'])
            wire = (folder/(label+'-wire.bin')).read_bytes()
            parsed = (a.read_sse if body['stream'] else a.read_json)(io.BytesIO(wire))
            pkg.require(asdict(parsed) == pkg.read(folder/(label+'-response.json')) and
                        parsed.id == body['request_id'], 'response/wire differs')
            if label in ('plain', 'stream'):
                pkg.require(parsed.message['content'].strip() == '710003', 'ordinary quality')
            if label == 'strict':
                pkg.require(json.loads(parsed.message['content']) == {'ok':True}, 'strict quality')
            if label != 'project-read':
                pkg.require(parsed.finish_reason == 'stop', 'unexpected finish')
        call = pkg.read(folder/'project-tool-started.json')
        pkg.require(call['arguments'] == dict(path='docs/MODEL.md', start_line=1, line_count=24),
                    'tool arguments differ')
        tool = pkg.read(folder/'project-tool-result.json')
        value = json.loads(tool['content'])
        pkg.require(value['ok'] and tool['tool_call_id'] == call['call_id'], 'tool result mismatch')
        answer = pkg.read(folder/'project-answer-response.json')['message']['content']
        a.validate_answer(answer, {value['source']['citation']}, 1)
        pkg.require('Qwen4ExpForConditionalGeneration' in answer, 'project answer quality')
        metrics = pkg.read(folder/'idle-metrics.json')['metrics']
        a.healthy_idle(metrics, number*5)
        pkg.require(metrics['q4t_structured_workers_started_total'] == number*3,
                    'grammar accounting mismatch')
        quiescent.append(evidence.values(pkg.read(folder/'quiescent.json')))
    if stage.PROFILE == 'day':
        hour = pkg.read(stage.OUT/'first-hour.json')
        pkg.require(hour == stage.hourly_checkpoint(240, hour['seconds'], quiescent[:240]),
                    'first-hour evidence differs')
        pkg.require(soak['envelope'] == evidence.envelope(quiescent), 'envelope differs')
    else:
        pkg.require(soak['envelope'] is None and not (stage.OUT/'first-hour.json').exists(),
                    'short run claimed long-duration proof')
    gpu = pkg.read(stage.OUT/'gpu-final.json')
    pkg.require(gpu['status'] == 'OK' and not gpu['pids'], 'GPU cleanup missing')
    return dict(passed=True, profile=stage.PROFILE, cycles=stage.CYCLES,
        successful_requests=len(request_ids), real_tool_executions=stage.CYCLES,
        guard=guard, resource_samples=capture['resource_samples'],
        day_scale_stability_proved=stage.PROFILE == 'day', new_model_or_http=0,
        manifest_sha256=plan['package']['manifest_sha256'])


def verify_archive():
    h.verify_seal(DEST)
    manifest = pkg.read(DEST/'manifest.json')
    expected = {r['member']:r for r in manifest['records']}
    seen = set()
    with tarfile.open(DEST/'raw-records.tar.gz', 'r:gz') as archive:
        for member in archive:
            pkg.require(member.isfile() and member.name in expected and member.name not in seen,
                        'unexpected archive member')
            row, raw = expected[member.name], archive.extractfile(member).read()
            pkg.require(len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row['sha256'],
                        'archive bytes differ')
            h.check_identity(row);seen.add(member.name)
    pkg.require(seen == expected.keys(), 'archive incomplete')
    for row in manifest['local_artifacts']:h.check_identity(row)
    for name, row in manifest['aliases'].items():
        h.check_identity(row)
        pkg.require(pkg.sha(DEST/name) == row['sha256'], 'archive alias differs')
    result = check()
    result.update(records=len(seen), local_artifacts=len(manifest['local_artifacts']))
    return result


def create_archive():
    pkg.require(stage.PROFILE == 'short', 'automatic Git archive is short-run only')
    result = check()
    pkg.require(not (stage.PHASE/'day/execution').exists(), 'day was unexpectedly started')
    unit = 'soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    mask = Path.home()/'.config/systemd/user'/unit
    pkg.require(mask.is_symlink() and os.readlink(mask) == '/dev/null', 'SOAR mask missing')
    state = subprocess.check_output(['systemctl','--user','show',unit,'-p','MainPID',
        '-p','ActiveState','-p','UnitFileState'], text=True, timeout=10)
    pkg.require(all(x in state for x in ('MainPID=0','ActiveState=inactive','UnitFileState=masked')),
                'SOAR state changed')
    lock = a.guard.common_lock()
    pkg.require(a.lock_available(lock) and lock.stat().st_ino == 3294067 and
        not os.path.lexists(lock.with_name(lock.name+'.recovery-required')), 'GPU lock/recovery pending')
    h.save(stage.PHASE/'short-verified.json', result)
    h.save(stage.PHASE/'handoff.json', dict(short_passed=True, day_executed=False,
        day_plan=h.identity(stage.PHASE/'day/plan.json'),
        long_validation_owner='user', automatic_execution=False,
        default_pair_unchanged=True, short_result=h.identity(stage.OUT/'result.json')))
    h.save(stage.PHASE/'postflight.json', dict(passed=True, soar_unit=state,
        lock_inode=lock.stat().st_ino, lock_available=True, recovery_marker=False,
        gpu=h.identity(stage.OUT/'gpu-final.json'), new_model_or_http=0))
    h.seal(stage.OUT)
    DEST.mkdir()
    records = []
    with (DEST/'raw-records.tar.gz').open('xb') as output:
        with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode='w|') as archive:
                for path in sorted(stage.PHASE.rglob('*')):
                    if not path.is_file():continue
                    pkg.require(not path.is_symlink(), 'unexpected evidence symlink')
                    raw, row = path.read_bytes(), h.identity(path)
                    member = str(path.relative_to(stage.PHASE))
                    entry = tarfile.TarInfo(member)
                    entry.size, entry.mode, entry.mtime = len(raw), 0o600, 0
                    archive.addfile(entry, io.BytesIO(raw))
                    records.append(dict(**row, member=member))
    plan = pkg.read(stage.PLAN)
    package = Path(plan['package']['package'])
    local = [h.identity(package/'q4t'), h.identity(package/'manifest.json')]
    aliases = {name+'.json':h.identity(path) for name, path in dict(
        short_plan=stage.PLAN, day_plan=stage.PHASE/'day/plan.json',
        result=stage.OUT/'result.json', cpu=stage.CPU/'result.json',
        verified=stage.PHASE/'short-verified.json', handoff=stage.PHASE/'handoff.json',
        postflight=stage.PHASE/'postflight.json').items()}
    for name, row in aliases.items():h.copy_checked(row, DEST/name)
    h.save(DEST/'manifest.json', dict(records=records, aliases=aliases,
        local_artifacts=local, archiver=h.identity(Path(__file__)),
        scope='Agent short validation only. The independent day plan is prepared for the user; '
              'no hour/day stability or physical RAM hard limit is accepted.'))
    h.seal(DEST)
    return verify_archive()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'archive-short', 'verify-archive'))
    parser.add_argument('--profile', choices=tuple(stage.PROFILES), default='short')
    args = parser.parse_args()
    stage.configure(args.profile)
    print(json.dumps(check() if args.action == 'check' else
        create_archive() if args.action == 'archive-short' else verify_archive()))
