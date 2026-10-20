"""Fixed-route LRU locality screen; not an async cache or latency simulator."""
import argparse
from collections import Counter, OrderedDict, defaultdict
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time

from offload_replay import ForwardTrace, ReplayConfig, replay
from offload_trace import iter_layers, validate_request
from offload_working_set import contiguous_next_by_layer, validate_layout
from run_offload_study import check_identity, token_order

ROOT = Path(__file__).resolve().parents[2]
STAGING_BYTES = 3276816


class LocalityScreen:
    def __init__(self):
        self.shared = OrderedDict()
        self.local = defaultdict(OrderedDict)
        self.counts = defaultdict(Counter)
        self.sequence = hashlib.sha256()

    @staticmethod
    def touch(cache, key, capacity):
        hit = key in cache
        cache[key] = None
        cache.move_to_end(key)
        if len(cache) > capacity:
            cache.popitem(last=False)
        return hit

    def __call__(self, event):
        # Each baseline source claim represents one planned GPU load.
        if event.kind not in ('nvme_read', 'l2_hit', 'mirror_hit'):
            return
        key = (event.layer, event.expert)
        self.sequence.update(f'{event.request_id}:{event.runtime_phase}:{key};'.encode())
        shared_hit = self.touch(self.shared, key, 768)
        local_hit = self.touch(self.local[event.layer], event.expert, 16)
        row = self.counts[(event.request_id, event.runtime_phase)]
        row.update(load_requests=1, shared_hits=int(shared_hit), local_hits=int(local_hit))
        if len(self.shared) > 768 or sum(map(len, self.local.values())) > 768:
            raise RuntimeError('payload slot budget exceeded')

    def finish(self):
        return dict(sequence_sha256=self.sequence.hexdigest(),
                    rows=[dict(request=request, phase=phase, **dict(counts))
                          for (request, phase), counts in self.counts.items()],
                    shared_slots=len(self.shared), local_slots=sum(map(len, self.local.values())))


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def execute(plan_path, out):
    if not out.resolve().is_relative_to(ROOT / '.q4t-work'):
        raise ValueError('output outside work artifacts')
    out.mkdir(parents=True, exist_ok=False)
    plan = json.loads(plan_path.read_text())
    old = json.loads(check_identity(plan['historical_plan']).read_text())
    layout_path = check_identity(plan['layout'])
    layout = json.loads(layout_path.read_text())
    save(out / 'layout-validation.json', validate_layout(layout))
    checker = check_identity(old['checker'])
    binary = check_identity(old['source_binary'])
    helper = check_identity(old['sort_helper'])
    for item in old['requests']:
        check_identity(item)
    require = lambda x: x or (_ for _ in ()).throw(ValueError('frozen screen changed'))
    require(plan['schedules'] == ['serial_eager', 'batch_deferred'])
    require(plan['slots'] == dict(local_per_layer=16, layers=48, shared=768))
    require(plan['staging_bytes'] == STAGING_BYTES)
    save(out / 'plan.json', plan)
    save(out / 'sources.json', {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(__file__).parent.glob('*.py')})
    state = dict(status='RUNNING', started=time.time(), completed=[])
    results = []
    try:
        for item in old['requests']:
            source = check_identity(item)
            validation = validate_request(source, checker=checker, source_binary=binary,
                                          expected_sha256=item['sha256'])
            if (validation['manifest']['model_index_sha256'] != layout['model_index_sha256'] or
                    validation['manifest']['model_config_sha256'] != layout['config_sha256']):
                raise ValueError('route/model identity mismatch')
            save(out / (item['name'] + '-source.json'), validation)
            forwards = []
            for layer in iter_layers(source, validation):
                ids = layer['topk_ids']
                forwards.append(ForwardTrace(layer=layer['layer'], forward_id=str(layer['forward_id']),
                    request_id=item['name'], phase='prefill' if layer['stage'] == 1 else 'decode',
                    topk_ids=ids, token_order=token_order(ids, layer['rows'], helper)))
            for schedule in plan['schedules']:
                name = item['name'] + '-' + schedule
                print('screen', name, flush=True)
                config = ReplayConfig(schedule=schedule,
                    contiguous_next_by_layer=contiguous_next_by_layer(layout))
                observer = LocalityScreen()
                def sequence():
                    for repeat in range(item['repeats']):
                        for forward in forwards:
                            yield replace(forward, request_id=item['name'] + '-r' + str(repeat + 1))
                result = replay(sequence(), config, {i: [] for i in range(48)}, observer=observer)
                screened = observer.finish()
                if sum(row['load_requests'] for row in screened['rows']) != result.aggregate['gpu_loads']:
                    raise ValueError('load sequence accounting mismatch')
                row = dict(name=name, baseline_replay=result.aggregate, locality=screened)
                results.append(row)
                save(out / (name + '.json'), row)
                state['completed'].append(name)
            check_identity(item)
        save(out / 'results.json', results)
        state['status'] = 'COMPLETE_LOCALITY_SCREEN_ONLY'
    except BaseException as error:
        state['error'] = repr(error)
        raise
    finally:
        state['finished'] = time.time()
        save(out / 'state.json', state)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    execute(args.plan, args.output)
