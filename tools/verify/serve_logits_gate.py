"""Require the full capacity candidate HTTP evidence before lower-level work."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def require_http(run):
    def read(name):
        return json.loads((run / name).read_text())

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    gate = read('acceptance.json')
    assert gate['quality_passed'] and gate['performance_accepted']
    sha = digest(ROOT / 'build/q4t')
    assert sha == gate['binary_sha256']
    assert read('consumer-http-audit.json')['http_gate_accepted']
    for mode, count in [('quality', 11), ('performance', 5)]:
        terminal = read(mode + '/exit.json')
        assert terminal['completed'] == count and terminal['server'] == 0
        assert terminal['failure'] is None and terminal['http_output_checks_passed']
        assert (run / mode / 'binary.sha256').read_text().strip() == sha
    for source in (run / 'source').rglob('*'):
        if source.is_file():
            assert source.read_bytes() == (ROOT / source.relative_to(run / 'source')).read_bytes()
    libraries = read('libraries.json')
    for name, expected in libraries.items():
        assert digest(ROOT / name) == expected
    batch = read('batch-http/review.json')
    assert batch['http_outputs_passed'] and batch['batch_coverage_passed']
    assert not any(x.get('adverse', False) for x in batch['comparisons'])
    assert read('interleave/review.json')['accepted']
    lifecycle = read('lifecycle/complete.json')
    assert lifecycle['passed'] == 4 and lifecycle['binary_sha256'] == sha
    for name in ['inline', 'vision', 'fallback-confirm', 'mtp-confirm-8k']:
        assert read('consumers-' + name + '/review.json')['accepted']
    for name in ['fallback', 'mtp']:
        joint = read(name + '-joint-review.json')
        assert joint['http_gate_accepted']
        assert read(joint['initial_review'])['http_outputs_passed']
        assert read(joint['confirmation_review'])['accepted']
    for name in ['inline', 'vision', 'fallback', 'fallback-confirm', 'mtp', 'mtp-confirm-8k']:
        for group in ['parent-before', 'candidate', 'parent-after']:
            prefix = 'consumers-' + name + '/' + group
            assert read(prefix + '/exit.json')['server'] == 0
            expected = sha if group == 'candidate' else digest(run / 'q4t-before')
            assert (run / prefix / 'binary.sha256').read_text().strip() == expected
    return sha, libraries
