"""Preserve CPU contracts and pinned official PromQL evaluation evidence."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_watchdog import core as c, watchdog as w
from tools.serve_watchdog.storage import encoded

BASE = ROOT/'.q4t-work/serve-watchdog-20261104'
RULE = 'deploy/serve-ops/q4t-watchdog.rules.yml'
SOURCES = (*w.FILES, RULE, 'tools/serve_watchdog/test_watchdog.py',
    'tools/serve_watchdog/cpu.py', 'tools/serve_watchdog/README.md',
    'docs/SERVE_NOTIFY_WATCHDOG_2026-11-04.md')
PROMTOOL = ROOT/'.q4t-work/serve-ops-20261103/deps/promtool'
PROMTOOL_SHA = 'd4ff433304144fae3d4490bd381be983e99064c601bc35e8dfe29f52300a9bc4'
SUMMARIES = dict(
    Q4THeartbeatMissing='The notification heartbeat is missing, resolved or unknown.',
    Q4TWatchdogEvaluationStale='The independent heartbeat observer is stale or clock-skewed.',
    Q4TWatchdogCollectionMissing='No independent heartbeat observation is being collected.')


def rule_tests():
    cases = []

    def case(name, state, stamp, when, alerts):
        series = []
        if state:
            series.append(dict(series='q4t_watchdog_state{deployment="thor-test",'
                               f'state="{state}"}}', values='1+0x20'))
        if stamp is not None:
            series.append(dict(series='q4t_watchdog_evaluated_timestamp_seconds'
                '{deployment="thor-test"}', values=stamp))
        checks = []
        for alert in SUMMARIES:
            expected = []
            if alert in alerts:
                labels = dict(severity='critical', component='q4t-watchdog')
                if alert != 'Q4TWatchdogCollectionMissing':
                    labels['deployment'] = 'thor-test'
                if alert == 'Q4THeartbeatMissing': labels['state'] = state
                expected.append(dict(exp_labels=labels,
                                     exp_annotations=dict(summary=SUMMARIES[alert])))
            checks.append(dict(eval_time=when, alertname=alert,
                               exp_alerts=expected))
        cases.append(dict(name=name, interval='5s', input_series=series,
                          alert_rule_test=checks))

    for state in ('PRESENT', 'WAITING'):
        case('healthy-evaluation-'+state, state, '0+5x20', '30s', [])
    for state in ('MISSING', 'RESOLVED', 'UNKNOWN'):
        case('unhealthy-'+state, state, '0+5x20', '30s', ['Q4THeartbeatMissing'])
    case('stale-boundary', 'PRESENT', '0+0x20', '15s', [])
    case('watcher-stopped', 'PRESENT', '0+0x20', '20s', ['Q4TWatchdogEvaluationStale'])
    case('future-clock-boundary', 'PRESENT', '50+0x20', '20s', [])
    case('future-clock', 'PRESENT', '51+0x20', '20s', ['Q4TWatchdogEvaluationStale'])
    case('collection-absent', None, None, '20s', ['Q4TWatchdogCollectionMissing'])
    # A resolved condition clears without an invented extra hold period.
    recovery = dict(name='missing-recovers', interval='5s', input_series=[
        dict(series='q4t_watchdog_state{deployment="thor-test",state="MISSING"}',
             values='1 1 0 0'),
        dict(series='q4t_watchdog_evaluated_timestamp_seconds{deployment="thor-test"}',
             values='0 5 10 15')], alert_rule_test=[dict(eval_time='15s',
                alertname=name, exp_alerts=[]) for name in SUMMARIES])
    cases.append(recovery)
    return dict(rule_files=[str(ROOT/RULE)], evaluation_interval='5s', tests=cases)


def run(output, names):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    sources = {name:w.file_sha(ROOT/name) for name in SOURCES}
    for name in SOURCES:
        dest = output/'source'/name; dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT/name).read_bytes())
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(names))
    (output/'tests.log').write_text(stream.getvalue())
    c.require(w.file_sha(PROMTOOL) == PROMTOOL_SHA, 'promtool_identity')
    fixture = rule_tests()
    (output/'rule-tests.json').write_bytes(encoded(fixture))
    commands = [([PROMTOOL, 'check', 'rules', ROOT/RULE], None),
        ([PROMTOOL, 'test', 'rules', output/'rule-tests.json'], None),
        ([PROMTOOL, 'check', 'metrics'], c.metrics(
            c.status(c.new_state('a'*64, w.now()), w.now()), 'thor-test')),
        ([sys.executable, '-I', '-B', ROOT/'tools/serve_watchdog/watchdog.py',
          '--help'], None)]
    rows = []
    for index, (argv, data) in enumerate(commands):
        p = subprocess.run(list(map(str, argv)), input=data, capture_output=True,
                           text=True, timeout=30)
        row = dict(argv=list(map(str, argv)), returncode=p.returncode,
                   stdout=p.stdout, stderr=p.stderr)
        rows.append(row)
        (output/f'check-{index}.json').write_bytes(encoded(row))
    value = dict(passed=result.wasSuccessful() and all(
        row['returncode'] == 0 for row in rows), tests_run=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), tests=names,
        official_rule_scenarios=len(fixture['tests']), sources=sources,
        promtool_sha256=PROMTOOL_SHA, model_gpu_http_started=False)
    (output/'result.json').write_bytes(encoded(value))
    print(stream.getvalue(), flush=True)
    print(json.dumps({k:v for k,v in value.items() if k != 'sources'}))
    c.require(value['passed'], 'CPU_failed_preserve_originals')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE/'cpu')
    parser.add_argument('tests', nargs='*', default=['tools.serve_watchdog.test_watchdog'])
    args = parser.parse_args()
    run(args.output, args.tests)
