"""Project closed observer reports; no trace parsing or new inference."""
import argparse
import hashlib
import json
from pathlib import Path
import time

R = Path(__file__).resolve().parent
OLD = R.parent / 'offload-supply-20261006'
GROUPS = ['s02-as-on', 's03-al-on']
EVENTS = [('prefill_begin', 0), ('prefill_end_decode_begin', 0),
          ('decode_prefix', 1), ('decode_prefix', 8),
          ('decode_prefix', 32), ('inference_end', 255)]
ALIASES = dict(prefill_lookups='shape_multi_lookups',
               decode_lookups='shape_single_lookups',
               prefill_misses='shape_multi_misses',
               decode_misses='shape_single_misses')
FIELDS = ('layer', 'slot_experts', 'slot_ticks', 'slot_protected',
          'slot_clock', 'l2_experts', 'l2_ticks', 'l2_clock',
          'mirror_experts', 'mirror_cursor')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def stat(path):
    s = Path(path).stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scope-sha256', required=True)
    parser.add_argument('--self-sha256', required=True)
    args = parser.parse_args()
    assert sha(__file__) == args.self_sha256
    scope_path = R / 'scope-plan.json'
    assert sha(scope_path) == args.scope_sha256
    scope = json.loads(scope_path.read_text())
    assert scope['groups'] == GROUPS and scope['new_HTTP'] == 0
    sources = {str(scope_path): args.scope_sha256,
               str(Path(__file__)): args.self_sha256}
    for p, h in scope['source_sha256'].items():
        assert sha(p) == h, p
        sources[p] = h
    review = json.loads((OLD/'observer-result-independent-review.json').read_text())
    assert review['passed'] and not review['blocking_findings']
    plan = json.loads((OLD/'observer-execution-plan.json').read_text())
    assert review['plan_sha256'] == sha(OLD/'observer-execution-plan.json')
    result = dict(schema=1, scope_sha256=args.scope_sha256,
        runtime_source_commit=plan['runtime_source_commit'],
        runtime_binary_path=plan['runtime_binary_path'],
        runtime_binary_sha256=plan['runtime_binary_sha256'], groups=[],
        extraction_trace_payloads_parsed=0, new_model_runs=0, new_HTTP=0,
        prior_parser_proof='Original complete observer decision and independent review reused; no new C++ checker execution.')
    for group in GROUPS:
        path = OLD/(group+'-decision.json')
        d = json.loads(path.read_text())
        assert d['passed'] and d['request_count'] == 4 and d['group_id'] == group
        assert d['runtime_source_commit'] == plan['runtime_source_commit']
        assert d['runtime_binary_sha256'] == plan['runtime_binary_sha256']
        assert review['source_sha256'][str(path)] == sha(path)
        assert d['supply']['observer_enabled'] is True
        assert len(d['traces']) == len(d['supply']['requests']) == 4
        g = dict(id=group, requests=[])
        result['groups'].append(g)
        previous_forward = 0
        previous_end = None
        for k, (q, trace) in enumerate(zip(d['supply']['requests'], d['traces'])):
            phase = q['phase_record']; input_tokens = 8193 if group == GROUPS[1] and k == 0 else 1024
            assert q['actual_input'] == phase['input_tokens'] == input_tokens
            assert q['actual_output'] == phase['output_tokens'] == 256
            assert phase['decode_forwards_completed'] == 255
            assert trace['metadata']['http_id'] == q['response_id']
            assert trace['metadata']['request_id'] == k+1
            assert trace['summary']['decode_rows'] == 255
            assert trace['summary']['prefill_rows'] == input_tokens
            assert trace['summary']['output_tokens'] == 256
            wanted = [(1, 0, min(8192, input_tokens))]
            if input_tokens > 8192:
                wanted.append((1, 8192, 1))
            wanted += [(2, input_tokens+i, 1) for i in range(255)]
            assert [(f['stage'], f['position'], f['rows']) for f in trace['forwards']] == wanted
            assert trace['forwards'][0]['forward_id'] > previous_forward
            assert all(a['forward_id'] < b['forward_id'] for a,b in zip(trace['forwards'], trace['forwards'][1:]))
            previous_forward = trace['forwards'][-1]['forward_id']
            tp = Path(trace['path']); bindings = {}
            for p in [tp, tp.with_suffix('.json'), tp.with_suffix('.tokens'), tp.parent/'manifest.json']:
                h = d['source_sha256'][str(p)]
                before = stat(p); assert sha(p) == h and stat(p) == before
                sources[str(p)] = h
                bindings[str(p)] = dict(sha256=h, stat=before)
            assert json.loads(tp.with_suffix('.json').read_text()) == trace['metadata']
            assert json.loads((tp.parent/'manifest.json').read_text()) == d['trace_manifest']
            assert tp.with_suffix('.tokens').stat().st_size == input_tokens*4
            validation = dict(path=str(tp), manifest=d['trace_manifest'], metadata=trace['metadata'],
                              summary=trace['summary'], bindings=bindings)
            snapshots = phase['snapshots']
            assert [(s['event'], s['decode_forward_count']) for s in snapshots] == EVENTS
            projected = []
            for j,s in enumerate(snapshots):
                full = j in (0,1,5)
                assert s['gpu_work_complete'] is True
                assert s['cache_state'] == ('full' if full else 'omitted')
                assert (len(s['layers']) == 48) if full else (s['layers'] is None)
                layers = [{f:layer[f] for f in FIELDS} for layer in s['layers']] if full else None
                if full:
                    assert [x['layer'] for x in layers] == list(range(48))
                counters = dict(s['stats'])
                for alias,raw in ALIASES.items(): counters[alias] = counters[raw]
                assert all(type(v) is int and v >= 0 for v in counters.values())
                projected.append(dict(event=s['event'], decode_forward_count=s['decode_forward_count'],
                    decode_forwards=s['decode_forward_count'], stats=s['stats'], counters=counters,
                    layers=layers, gpu_work_complete=True, cache_state=s['cache_state']))
            if previous_end is not None:
                assert projected[0]['layers'] == previous_end
            previous_end = projected[5]['layers']
            observed = q['supply_observer']['decode']
            assert observed['scope'] == 'actual_decode'
            assert [x['layer'] for x in observed['layers']] == list(range(48))
            g['requests'].append(dict(position=k, response_id=q['response_id'], input_tokens=input_tokens,
                output_tokens=256, trace_path=str(tp), validation=validation, forwards=trace['forwards'],
                snapshots=projected, observer_decode=observed))
    for p,h in sources.items(): assert sha(p) == h, p
    result.update(source_sha256=sources, recorded_t=time.time())
    out = R/'retention-inputs.json'
    with out.open('x') as f:json.dump(result,f,ensure_ascii=False,separators=(',', ':'),allow_nan=False);f.write('\n')
    print(json.dumps(dict(passed=True,sha256=sha(out),bytes=out.stat().st_size,requests=8,trace_payloads_parsed=0)))


if __name__ == '__main__':
    main()
