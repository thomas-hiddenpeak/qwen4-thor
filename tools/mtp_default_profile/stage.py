"""Bounded observation of the accepted ELF, without a runtime candidate."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import signal
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'default_profile_http', ROOT / 'tools/mtp_selected_head/stage.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
legacy, telemetry = base.legacy, base.telemetry
sha, save = base.sha, base.save

MODES = {
    'normal': {},
    'coarse': {'Q4T_MTP_TIMING': '1', 'Q4T_LAYER_TIMING': '1'},
    # Layer timing's head interval includes sub-timer Report/Destroy work.
    # Do not enable it here or mislabel observer work as head computation.
    'fine': {'Q4T_MTP_TIMING': '1', 'Q4T_LAYER_SUBTIMING': '1'},
}


def validate(plan):
    expected = [(m, h) for m in MODES for h in ('full', 'subset')]
    if (plan['stage'] != 'DEFAULT_VERIFY_PROFILE' or
            [(s['mode'], s['head']) for s in plan['steps']] != expected or
            (plan['http_requests'], plan['http_loads']) != (12, 6) or
            plan['stage_timeout_seconds'] != 1800):
        raise ValueError('frozen observation scope')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute deadline')
    for step in plan['steps']:
        if (step['http_requests'], step['http_loads']) != (2, 1):
            raise ValueError('per-service budget')
        if [(c['id'], c['count'], c['tokens'], c['parallel'])
                for c in step['cases']] != [
                    ('context-1024', 1, 256, 1),
                    ('context-45056', 1, 256, 1)]:
            raise ValueError('case budget')
        if step['server_command'][0] != plan['candidate']:
            raise ValueError('only accepted frozen ELF may execute')


def custom_step(plan, name):
    step = next(s for s in plan['steps'] if s['name'] == name)
    clean = legacy.clean_env
    try:
        # This process owns exactly one step; restore the shared callback.
        legacy.clean_env = lambda: {
            k: v for k, v in clean().items()
            if not k.startswith(('CUPTI_', 'NSYS_', 'NVTX_'))
            and k != 'CUDA_INJECTION64_PATH'
        } | MODES[step['mode']]
        base.custom_step(plan, name)
    finally:
        legacy.clean_env = clean


def audit_server(out, expected):
    result = base.audit_server(out, expected)
    raw = (out / 'server.log').read_text()
    step = json.loads((out / 'step.json').read_text())
    env = json.loads((out / 'server-command.json').read_text())['environment']
    if env != MODES[step['mode']]:
        raise RuntimeError('observation environment changed')
    for marker in ('sync failed:', 'overflow=1', 'clock_error=1'):
        if marker in raw:
            raise RuntimeError('observer error: ' + marker)
    mtp = re.findall(r'^\[mtp-timing\].*$', raw, re.M)
    if step['mode'] == 'normal':
        if mtp or '[sub-timing]' in raw or '[layer-timing]' in raw:
            raise RuntimeError('normal HTTP contaminated by timers')
    elif not mtp or any(' gpu[ ' not in line for line in mtp):
        raise RuntimeError('missing/failed MTP event intervals')
    if step['mode'] == 'coarse' and '[layer-timing] T=4 ' not in raw:
        raise RuntimeError('missing verify layer observations')
    if step['mode'] == 'fine':
        if '[sub-timing] T=4 ' not in raw or '[moe-subtiming] T=4 ' not in raw:
            raise RuntimeError('missing verify detail observations')
        if '[layer-timing]' in raw:
            raise RuntimeError('fine head interval would include report overhead')
    counts = re.findall(r'^\[mtp-accept\].* draft_counts=([0-9,]+)$', raw, re.M)
    if len(counts) != expected:
        raise RuntimeError('missing acceptance histogram')
    if step['mode'] != 'normal':
        name = '00-normal-full' if step['head'] == 'full' else '01-normal-subset'
        normal = (out.parent / name / 'server.log').read_text()
        reference = re.findall(
            r'^\[mtp-accept\].* draft_counts=([0-9,]+)$', normal, re.M)
        if counts != reference:
            raise RuntimeError('observer changed acceptance trajectory')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--step')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    validate(plan)
    if args.step:
        custom_step(plan, args.step)
    else:
        base.monitored_stage.run_stage(legacy, telemetry, plan, args.plan,
                                       audit_server=audit_server)
        print('COLLECTED_REQUIRES_OFFLINE_REVIEW')


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('default profile interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    main()
