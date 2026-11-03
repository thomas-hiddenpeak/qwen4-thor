"""Read-only evidence checks and lossless archival; no CUDA, model, or HTTP."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_cuda_quota import stage as s, correction as c

DEST = ROOT/'docs/evidence/serve-cuda-quota-core-20261104'


def evaluate(data):
    def read(name): return json.loads(data[name])
    def lines(name): return [json.loads(line) for line in data[name].splitlines()]
    plan = read('plan.json')
    corrected = read('correction-plan.json')
    s.require(plan['probe'] == corrected['probe'] and
        plan['serve_integrated'] is False and plan['physical_ram_hard_limit'] is False,
        'same native probe / bounded acceptance')
    for folder, count in (('cpu',25), ('cpu-v2',26)):
        build = read(folder+'/build-result.json')
        tests = read(folder+'/tests/result.json')
        s.require(build['passed'] and build['zero_warnings'] and tests['passed'] and
                  len(tests['cases']) == count, 'CPU outcomes')
        for path, digest in build['sources'].items():
            s.require(hashlib.sha256(data[folder+'/source/'+path]).hexdigest() == digest,
                      'CPU frozen source: '+path)
        raw = data[folder+'/tests/contracts.stdout'].decode()
        s.require(sum(line.startswith('PASS ') for line in raw.splitlines()) == count,
                  'raw host contract count')
        driver = data[folder+'/tests/driver.stderr'].decode()
        s.require('Ran 4 tests' in driver and driver.rstrip().endswith('OK'), 'driver result')
        s.require('cuda' not in data[folder+'/host-needed.stdout'].decode().lower(),
                  'CPU CUDA dependency')
    repro = read('publication-repro/reproduced.json')
    s.require(repro['expected_failure'] and repro['execution']['returncode'] == 1 and
        b'FAIL: allocation' in data['publication-repro/release-publication-race.stderr'],
        'original publication failure preserved')
    cpu = read('correction-cpu/result.json')
    s.require(cpu['passed'] and cpu['count'] == 6 and not cpu['errors'] and not cpu['failures'],
              'custody CPU result')
    for path, digest in cpu['sources'].items():
        s.require(hashlib.sha256(data['correction-cpu/source/'+path]).hexdigest() == digest,
                  'custody source snapshot')
    windows = {}
    for folder, lease, final in (('execution','lease',False), ('execution-v2','lease-v2',True)):
        result = read(folder+'/result.json')
        exit_status = read(lease+'/exit.json')
        events = lines(lease+'/events.jsonl')
        s.require(result['passed'] and result['elapsed_seconds'] < 90 and
                  result['model_starts'] == result['http_requests'] == 0,
                  'native driver local outcome')
        acquired = [e for e in events if e['event'] == 'lease_acquired']
        released = [e for e in events if e['event'] == 'lease_released']
        s.require(len(acquired) == len(released) == 1, 'lease lifecycle')
        seconds = released[0]['monotonic']-acquired[0]['monotonic']
        s.require(0 < seconds < 90, 'lease budget')
        observations = [e for e in events if e['event'] in ('preflight','monitor')]
        for index, mode in enumerate(s.MODES):
            raw = data[folder+'/'+mode+'/stdout.jsonl'].decode()
            parsed = s.parse_probe(mode, raw)
            s.require(parsed == read(folder+'/'+mode+'/result.json') == result['probes'][index],
                      'raw CUDA checkpoint identity')
            name = 'exit-waitable.json' if final else 'exit.json'
            ended = read(folder+'/'+mode+'/'+name)
            s.require(ended['returncode'] == 0 and ended['elapsed_seconds'] <= 15,
                      'native exit')
        if final:
            s.require(exit_status['status'] == released[0]['status'] == 'COMPLETED' and
                exit_status['child_returncode'] == 0 and result['all_children_reaped'] and
                not any(e['conflicts'] for e in observations), 'final guard outcome')
            begin = read(folder+'/begin.json')
            ack = read(folder+'/guard-acknowledgement.json')
            children = [read(folder+'/'+mode+'/started.json') for mode in s.MODES]
            s.require(ack['monitor'] in observations, 'actual guard observation')
            queries = lines(lease+'/nvml-queries.jsonl')
            matching = [q for q in queries if q['request_id'] == ack['request_id']]
            s.require(len(matching) == 1, 'ack query identity')
            checked = c.acknowledgement([ack['monitor']],matching,
                                        children[-1]['registered_monotonic'])
            s.require(checked is not None and checked['query_begin'] == ack['query_begin'] and
                      checked['query_end'] == ack['query_end'], 'ack temporal boundary')
            c.validate_zombies(children, ack['retained'], begin['pid'], begin['pgid'])
            for mode, child in zip(s.MODES, children):
                reaped = read(folder+'/'+mode+'/reaped.json')
                s.require(reaped['pid'] == child['pid'] and reaped['returncode'] == 0 and
                    reaped['monotonic'] >= ack['monitor']['monotonic'], 'reaped before observation')
        else:
            conflicts = [x for e in observations for x in e['conflicts']]
            native = read('execution/concurrent/started.json')
            s.require(exit_status['status'] == released[0]['status'] == 'ENV_ABORT' and
                exit_status['child_returncode'] == -2 and
                len(conflicts) == 1 and conflicts[0]['pid'] == native['pid'],
                'original ENV_ABORT classification changed')
        windows[lease] = dict(status=exit_status['status'], seconds=seconds,
                             observations=len(observations), records=len(events))
    post = read('postflight/result.json')
    s.require(post['passed'] and post['gpu_empty'] and post['containers_empty'] and
        post['lock']['available'] and post['lock']['inode'] == 3294067 and
        post['lock']['device'] == 66305 and post['recovery_mask_target'] == '/dev/null' and
        len(post['owned']) == 8 and all(r['owned_exited'] for r in post['owned']) and
        post['baseline'] == plan['baseline'], 'final ownership/default/day')
    s.require(not data['postflight/gpu.stdout'].strip() and
        not data['postflight/containers.stdout'].strip() and
        data['postflight/recovery-service.stdout'].decode().splitlines() ==
        ['LoadState=masked','ActiveState=inactive','SubState=dead'], 'postflight raw queries')
    return dict(status='PASS_CORE_ONLY', core_cpu_effective=26,
        core_cpu_executions=52, core_cpu_failures_preserved=1,
        initial_core_passes=25, final_core_passes=26,
        original_driver_checks=4, repeated_driver_checks=4, custody_cpu_checks=6,
        native_starts=8, accepted_native_starts=4, original_env_abort_preserved=True,
        accepted_verified_buffers=15, accepted_verified_bytes=112*1024*1024,
        accepted_quota_checkpoints=14, windows=windows,
        model_starts=0, http_requests=0, serve_integrated=False,
        physical_ram_hard_limit=False, baseline=post['baseline'])


def collect():
    data = {}
    directories = ('cpu','cpu-v2','publication-repro','correction-cpu',
                   'execution','execution-v2','lease','lease-v2','postflight','baseline')
    excluded = {'publication-repro/contracts','baseline/q4t'}
    for name in directories:
        for path in (s.BASE/name).rglob('*'):
            if path.is_file() and str(path.relative_to(s.BASE)) not in excluded:
                data[str(path.relative_to(s.BASE))] = path.read_bytes()
    for name in ('plan.json','correction-plan.json'):
        data[name] = (s.BASE/name).read_bytes()
    for directory in ('build','build-v2'):
        for name in ('CMakeCache.txt','compile_commands.json'):
            data[directory+'/'+name] = (s.BASE/directory/name).read_bytes()
    data['archiver/report.py'] = Path(__file__).read_bytes()
    return data


def check():
    c.check()
    return evaluate(collect())


def archive():
    c.check()
    baseline=s.BASE/'baseline'
    baseline.mkdir()
    for source,name in ((ROOT/'build/q4t','q4t'),
        (ROOT/'build/q4t.release.json','q4t.release.json'),
        (s.DAY/'plan.json','day-plan.json')):
        (baseline/name).write_bytes(source.read_bytes())
        s.require(s.sha(baseline/name)==s.sha(source),'baseline copy')
    data = collect()
    result = evaluate(data)
    DEST.mkdir(parents=True, exist_ok=False)
    manifest = {name:dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
                for name,raw in sorted(data.items())}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w',format=tarfile.PAX_FORMAT) as archive:
        for name,raw in sorted(data.items()):
            info = tarfile.TarInfo(name)
            info.size,info.mode,info.mtime = len(raw),0o600,0
            archive.addfile(info,io.BytesIO(raw))
    packed = gzip.compress(buffer.getvalue(),mtime=0)
    (DEST/'records.tar.gz').write_bytes(packed)
    local = {}
    for directory in ('build','build-v2'):
        for name in ('quota_host_contracts','quota_cuda_probe'):
            value=s.identity(s.BASE/directory/name);local[value['path']]=value
    for path in (s.BASE/'publication-repro/contracts', baseline/'q4t',
                 baseline/'q4t.release.json',baseline/'day-plan.json',Path('/usr/bin/g++-14')):
        value=s.identity(path);local[value['path']]=value
    for value in s.read(s.CPU/'build-result.json')['libraries'].values():
        local[value['path']]=value
    s.save(DEST/'manifest.json',dict(schema=1,files=manifest,
        archive=dict(name='records.tar.gz',bytes=len(packed),sha256=hashlib.sha256(packed).hexdigest()),
        archiver_sha256=s.sha(__file__),stage_sha256=s.sha(s.__file__),
        correction_sha256=s.sha(c.__file__),local_identities=local))
    s.save(DEST/'result.json',result)
    print(json.dumps(dict(records=len(data),local_identities=len(local),bytes=len(packed),result=result)))


def verify_archive():
    manifest=s.read(DEST/'manifest.json')
    s.require(manifest['archiver_sha256']==s.sha(__file__) and
        manifest['stage_sha256']==s.sha(s.__file__) and
        manifest['correction_sha256']==s.sha(c.__file__), 'verifier identity changed')
    raw=(DEST/'records.tar.gz').read_bytes()
    s.require(len(raw)==manifest['archive']['bytes'] and
        hashlib.sha256(raw).hexdigest()==manifest['archive']['sha256'], 'archive bytes')
    data={}
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
        for member in archive:
            s.require(member.isfile() and member.name not in data and
                not Path(member.name).is_absolute() and '..' not in Path(member.name).parts,
                'archive member')
            data[member.name]=archive.extractfile(member).read()
    actual={name:dict(bytes=len(value),sha256=hashlib.sha256(value).hexdigest())
            for name,value in data.items()}
    s.require(actual==manifest['files'], 'raw record bytes changed')
    s.require(evaluate(data)==s.read(DEST/'result.json'), 'archived semantics changed')
    for value in manifest['local_identities'].values():
        s.require(s.identity(value['path'])==value, 'retained binary/dependency identity')
    print('ARCHIVE_PASS',len(data),'records',len(manifest['local_identities']),'local identities')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['check','archive','verify-archive'])
    args=parser.parse_args()
    if args.action=='check': print(json.dumps(check(),indent=2))
    elif args.action=='archive': archive()
    else: verify_archive()
