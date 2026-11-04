"""Offline repair: an empty argv needs an earlier verified full identity.

Original samples, driver, production source and failed report stay immutable.
No process state is inferred from an empty argv and no model/HTTP is executed.
"""
import argparse
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
if __name__ == '__main__':
    sys.modules['tools.serve_port_preflight.finish'] = sys.modules[__name__]
from tools.serve_port_preflight import report as original

pkg, s = original.pkg, original.s
SOURCES = ('tools/serve_port_preflight/finish.py',
           'tools/serve_port_preflight/test_finish.py')


def audit_samples(samples, binding):
    pkg.require(bool(samples), 'host samples missing')
    policy = binding['record']['policy']
    limits = s.resources.limits(policy)
    relative = '/' + str(Path(binding['record']['root']).relative_to(s.resources.CGROUP))
    model = binding['owners']['backend']
    members, known_at, reused = {}, {}, []
    for index, row in enumerate(samples):
        pkg.require(row['effective_limits'] == limits and
            s.resources.violation(row) is None and
            row['memory.current'] <= policy['memory_max_bytes'] and
            row['pids.current'] <= policy['tasks_max'] and
            row['memory.swap.current'] == 0, 'live host limits')
        for member in row['processes']:
            owner, argv = member['owner'], member['argv']
            pid = owner['pid']
            pkg.require(member['membership'] == relative + '/payload', 'member escaped payload')
            if pid in members:
                pkg.require(members[pid]['owner'] == owner, 'member identity changed')
            if pid == model['pid']:
                pkg.require(owner == model, 'model identity changed')
            else:
                pkg.require(member['parent'] == model['pid'], 'worker parent changed')
                if argv:
                    pkg.require(len(argv) == 4 and argv[:2] ==
                        ['q4t-grammar-worker', '--q4t-grammar-worker'] and
                        argv[2] == str(model['pid']), 'unidentified payload descendant')
                    known_at[pid] = index
                else:
                    pkg.require(pid in known_at and pid in members and
                        members[pid]['owner'] == owner, 'empty argv lacks earlier role evidence')
                    reused.append(dict(sample_index=index, owner=owner,
                                       earlier_role_sample=known_at[pid]))
            members.setdefault(pid, member)
    pkg.require(model['pid'] in members and len(members) == 5,
                'model and four actual grammar workers must be observed')
    return dict(samples=len(samples), processes=list(members.values()),
        sampled_memory_peak_bytes=max(v['memory.peak'] for v in samples),
        sampled_tasks_peak=max(v['pids.current'] for v in samples),
        empty_argv_with_earlier_role_evidence=reused,
        original_process_state_recorded=False)


def cpu():
    out = s.BASE / 'report-cpu'
    out.mkdir(mode=0o700)
    suite = unittest.defaultTestLoader.loadTestsFromName(
        'tools.serve_port_preflight.test_finish.Observation')
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (out / 'tests.log').write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful() and not result.skipped,
        tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        skipped=len(result.skipped), sources={n: pkg.sha(ROOT / n) for n in SOURCES},
        additional_models=0, additional_http=0)
    pkg.save(out / 'result.json', value)
    pkg.require(value['passed'], log.getvalue())
    return value


def check():
    value = pkg.read(s.BASE / 'report-cpu/result.json')
    pkg.require(value['passed'] and value['tests_run'] == 4 and
        value['errors'] == value['failures'] == value['skipped'] == 0 and
        value['sources'] == {n: pkg.sha(ROOT / n) for n in SOURCES}, 'report repair CPU gate')
    failure = pkg.read(s.BASE / 'report-failure-01.json')
    pkg.require(failure['returncode'] != 0 and 'IndexError: list index out of range' in
                failure['stderr'], 'preserved original report failure')
    correction = pkg.read(s.BASE / 'report-correction.json')
    pkg.require(correction['source_files'] == value['sources'] and
        correction['cpu_sha256'] == pkg.sha(s.BASE / 'report-cpu/result.json') and
        correction['failure_sha256'] == pkg.sha(s.BASE / 'report-failure-01.json'),
        'correction identity')


def audit():
    check()
    result = original.audit()
    result['report_repair_cpu_checks'] = 4
    result['report_repair'] = 'Reuse earlier role evidence for unchanged full identity; empty argv alone never identifies a worker.'
    return result


def configure():
    original.configure()
    original.host.audit_samples = audit_samples
    original.prior.audit = audit


def archive():
    value = pkg.read(s.BASE / 'report-cpu/result.json')
    pkg.save(s.BASE / 'report-correction.json', dict(source_files=value['sources'],
        cpu_sha256=pkg.sha(s.BASE / 'report-cpu/result.json'),
        failure_sha256=pkg.sha(s.BASE / 'report-failure-01.json'),
        rule='Only prior matching pid/start_ticks/boot_id with actual grammar argv can identify an empty-argv observation; keep every resource and membership check.',
        preserved_original_samples=True, process_state_not_inferred=True,
        additional_models=0, additional_http=0, additional_gpu=0))
    for name in SOURCES:
        path = s.BASE / 'reviewed-source' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write((ROOT / name).read_bytes())
    # The first attempt already preserved the original report/driver snapshots.
    for path in (s.BASE / 'reviewed-source').rglob('*'):
        if path.is_file():
            pkg.require(pkg.sha(path) == pkg.sha(ROOT / path.relative_to(s.BASE / 'reviewed-source')),
                        'reviewed source drift')
    original.prior.archive()


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('cpu', 'archive', 'verify'))
    action = parser.parse_args().action
    configure()
    if action == 'verify':
        original.prior.verify()
    elif action == 'cpu':
        print(json.dumps(cpu()))
    else:
        archive()
