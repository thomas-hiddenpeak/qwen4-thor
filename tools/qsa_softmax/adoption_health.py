"""Conditional installed QSA health: two heads, three cache requests each."""
import argparse
import importlib.util
from pathlib import Path
import signal
import time
import urllib.request

spec = importlib.util.spec_from_file_location('adoption_cache_health',
    Path(__file__).with_name('adoption_cache.py'))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
a, driver = c.a, c.driver
prior = a.module('adoption_health_cases', 'tools/session_incremental/health.py')


def validate(plan, executing=True):
    a.require((plan['stage'], plan['http_loads'], plan['http_requests'],
        plan['stage_timeout_seconds'], plan['approved_total_wall_seconds']) ==
        ('QSA_SOFTMAX_ADOPTION_HEALTH', 2, 6, 840, 900), 'delivery health budget')
    a.require([s['head'] for s in plan['steps']] == ['full', 'subset'], 'two heads')
    for step in plan['steps']:
        a.require((step['kind'], step['http_loads'], step['http_requests'],
            step['timeout'], step['layout'], step['suffix'], step['binary']) ==
            ('lifecycle', 1, 3, 420, 'agent', True, plan['candidate']),
            'installed health step')
    gate = a.read(plan['required_gate'])
    a.require(gate['status'] == 'PASS_QSA_BOUNDED_ENGINEERING_ACCEPTANCE' and
        gate['passed'] and gate['candidate_sha256'] == a.CANDIDATE,
        'explicit completed engineering acceptance')
    if executing:
        a.require(time.time() < plan['task_deadline_unix'], 'health deadline')


def lifecycle(plan, out, check):
    for name, route in [('capabilities', '/v1/capabilities'),
                        ('configuration', '/v1/config')]:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{plan['port']}" + route, timeout=10) as response:
            body = response.read()
        (out / (name + '.json')).write_bytes(body)
        a.read(out / (name + '.json'))
    caps = a.read(out / 'capabilities.json')
    a.require(caps['protocol_source_sha256'] == plan['protocol_sha256'],
              'actual installed protocol')
    prior.case(plan, a.read(out / 'step.json'), None, out, None, check)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--step')
    args = parser.parse_args()
    plan = a.read(args.plan)
    validate(plan)
    driver.lifecycle = lifecycle
    if args.step:
        driver.custom_step(plan, args.step)
    else:
        a.base.monitored_stage.run_stage(a.legacy, a.telemetry, plan, args.plan,
                                       audit_server=driver.audit)


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('installed QSA health interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    main()
