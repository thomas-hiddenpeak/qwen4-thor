"""Opt-in, single HTTP-window overcurrent count observer; never acceptance.

Reads soctherm_oc sysfs counters. Reuses the identity-checked EvalScope AST
boundary hooks; no server injection, clock writes or collector subprocess.
"""
import argparse
import json
from pathlib import Path
import sys
import time

from ucf_client_boundary import instrument


def snapshot():
    begin = time.monotonic_ns()
    devices = [p for p in Path('/sys/class/hwmon').glob('hwmon*')
               if (p / 'name').read_text().strip() == 'soctherm_oc']
    if len(devices) != 1:
        raise RuntimeError('one soctherm_oc device required')
    device = devices[0]
    values = {}
    for alarm in range(1, 4):
        for field in ('event_cnt', 'throt_en'):
            name = f'oc{alarm}_{field}'
            value = int((device / name).read_text())
            limit = 2**32 if field == 'event_cnt' else 2
            if not 0 <= value < limit:
                raise RuntimeError('invalid OC counter or enable value')
            values[name] = value
    return dict(begin_ns=begin, end_ns=time.monotonic_ns(),
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                device=str(device.resolve()), values=values)


class OcWindow:
    def __init__(self, evidence, reader=snapshot):
        self.evidence = Path(evidence)
        self.evidence.mkdir(parents=True, exist_ok=False)
        self.reader = reader
        self.rows = []
        self.complete = False
        self.started = False
        self.error = None
        self.boundary = None
        self.delta = None

    def save(self):
        data = dict(rows=self.rows, complete=self.complete, error=self.error,
                    boundary=self.boundary, delta=self.delta,
                    performance_acceptance=False,
                    scope='System-wide sequential reads; HTTP first output may '
                          'include early decode. Counts are not throttle duration.')
        (self.evidence / 'counter.json').write_text(json.dumps(data, indent=2)+'\n')

    def start(self):
        if self.started:
            raise RuntimeError('only one OC window permitted')
        self.started = True
        self.rows.append(self.reader())
        self.save()

    def first(self, request_start, first_output):
        if not self.started or self.complete or len(self.rows) != 1:
            raise RuntimeError('invalid OC first-output lifecycle')
        callback = time.monotonic_ns()
        self.rows.append(self.reader())
        a, b = self.rows
        if (a['boot_id'], a['device']) != (b['boot_id'], b['device']):
            raise RuntimeError('OC boot/device identity changed')
        delta = {}
        for alarm in range(1, 4):
            count, enable = f'oc{alarm}_event_cnt', f'oc{alarm}_throt_en'
            if a['values'][enable] != b['values'][enable]:
                raise RuntimeError('OC throttle enable changed')
            difference = b['values'][count] - a['values'][count]
            if difference < 0:
                raise RuntimeError('OC count decreased: reset/wrap not interpretable')
            delta[f'oc{alarm}'] = difference
        start, first = round(request_start * 1e9), round(first_output * 1e9)
        if not a['end_ns'] <= start <= first <= callback <= b['begin_ns']:
            raise RuntimeError('OC/client clock ordering invalid')
        self.boundary = dict(request_start_ns=start, first_output_ns=first,
                             callback_ns=callback,
                             leading_slack_ns=start-a['begin_ns'],
                             trailing_slack_ns=b['end_ns']-first,
                             start_read_ns=a['end_ns']-a['begin_ns'],
                             first_read_ns=b['end_ns']-b['begin_ns'])
        self.delta = delta
        self.complete = True
        self.save()

    def close(self, error=None):
        if error is not None:
            self.complete = False
            self.error = str(error)
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('client_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if time.get_clock_info('perf_counter').implementation != time.get_clock_info('monotonic').implementation:
        raise RuntimeError('client and observer clocks differ')
    window = OcWindow(args.evidence)
    from evalscope.perf.plugin.api.default_api import DefaultApiPlugin
    patched = instrument(DefaultApiPlugin.process_request, window.start, window.first)
    requests = 0

    async def observed(self, *a, **kw):
        nonlocal requests
        requests += 1
        try:
            if requests != 1:
                raise RuntimeError('OC observer requires exactly one request')
            result = await patched(self, *a, **kw)
            if not result.success or not window.complete:
                raise RuntimeError('HTTP/OC observation incomplete')
            return result
        except BaseException as error:
            window.close(error)
            raise
    DefaultApiPlugin.process_request = observed
    client_args = args.client_args[1:] if args.client_args[:1] == ['--'] else args.client_args
    sys.argv = ['evalscope', *client_args]
    try:
        from evalscope.cli.cli import run_cmd
        rc = run_cmd()
        if rc not in (None, 0) or not window.complete:
            raise RuntimeError('client/OC observation incomplete')
    except BaseException as error:
        window.close(error)
        raise
    else:
        window.close()


if __name__ == '__main__':
    main()
