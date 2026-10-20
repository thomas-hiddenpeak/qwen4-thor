"""Audit frozen locality outputs without claiming asynchronous cache correctness."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/shared-l2-20261020'
OUT = BASE / 'screen-v2'


def read(path):
    return json.loads(path.read_text())


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def main():
    require(read(OUT / 'state.json')['status'] == 'COMPLETE_LOCALITY_SCREEN_ONLY', 'completion')
    for name, digest in read(OUT / 'sources.json').items():
        require(sha(ROOT / name) == digest, 'source identity: ' + name)
    plan = read(OUT / 'plan.json')
    for key in ('historical_plan', 'layout'):
        item = plan[key]
        require(sha(Path(item['path'])) == item['sha256'], 'plan input identity')
    source = read(Path(plan['historical_plan']['path']))
    for item in [source['source_binary'], source['checker'], source['sort_helper'], *source['requests']]:
        require(sha(Path(item['path'])) == item['sha256'], 'source dependency identity')
    actual = []
    log = (BASE / 'capture-v2/run/server.log').read_text()
    for line in log.splitlines():
        if '[q4t][residency] id=' in line:
            actual.append(dict(re.findall(r'(\w+)=([^ ]+)', line)))
    require(len(actual) == 2, 'runtime request count')
    results = read(OUT / 'results.json')
    require(len(results) == 4, 'frozen variant count')
    compact = []
    positive = True
    for result in results:
        locality = result['locality']
        require(locality['shared_slots'] <= 768 and locality['local_slots'] <= 768, 'budget')
        require(sum(r['load_requests'] for r in locality['rows']) == result['baseline_replay']['gpu_loads'],
                'source-claim accounting')
        for row in locality['rows']:
            require(0 <= row['local_hits'] <= row['load_requests'] and
                    0 <= row['shared_hits'] <= row['load_requests'], 'hit bounds')
            if row['phase'] == 'prefill':
                positive &= row['shared_hits'] > row['local_hits']
            if row['request'] == 'calibration-retrieval-45056-r1':
                key = 'pmiss' if row['phase'] == 'prefill' else 'dmiss'
                require(row['load_requests'] == int(actual[0][key]), 'first calibration runtime closure')
        compact.append(dict(name=result['name'], **locality))
    require(positive, 'no common prefill locality opportunity')
    manifest = {str(p.relative_to(OUT)): sha(p) for p in OUT.rglob('*') if p.is_file()}
    print(json.dumps(dict(status='PASS_LOCALITY_SCREEN_ONLY', payload_slots=768,
        payload_bytes=768 * 3276816, variants=compact,
        first_calibration_gpu_loads_match_runtime=True,
        holdout_replay_starts_empty_unlike_capture_history=True,
        decision='PROCEED_TO_CLAIM_EVENT_MODEL_NOT_RUNTIME_DEPLOYMENT',
        schedules_may_have_identical_access_sequences=True,
        performance_acceptance=False, predicted_physical_ssd_bytes=None,
        long_decode_coverage=False, artifact_hashes=manifest), indent=2))


if __name__ == '__main__':
    main()
