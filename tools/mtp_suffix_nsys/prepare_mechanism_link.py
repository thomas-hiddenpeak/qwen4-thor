"""Prepare one original-production dependency correction; no workload change."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/suffix-memset-link-20261026'
SPEC = importlib.util.spec_from_file_location(
    'fixed_mechanism_preparation', Path(__file__).with_name('prepare_mechanism.py'))
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


def main():
    scope = json.loads((BASE/'scope.json').read_text())
    prepare.require(scope['classification'] == 'SPECIFIC_PRODUCTION_LINK_DEPENDENCY_FIX'
                    and scope['build_attempts'] == 1 and scope['cpu_suites'] == 0,
                    'only the frozen dependency correction is allowed')
    reused = scope['cpu_reuse']
    prepare.require(reused['methods'] == 55 and
        prepare.sha(reused['path']) == reused['sha256'] == prepare.sha(BASE/'cpu-proof.json'),
        'reused CPU proof identity changed')
    old = scope['prior_phase_result']
    prepare.require(prepare.sha(old['path']) == old['sha256'] and
        json.loads(Path(old['path']).read_text())['status'] == 'STOPPED_BUILD_DEPENDENCY',
        'prior build failure identity changed')
    build = json.loads((BASE/'build-proof.json').read_text())
    source = str(ROOT/'src/model/gemv_hc_inject.cu')
    prepare.require(build['source_attempts'] == 1 and
        source in build['actual_dependencies'] and build['files'][source] ==
        '171d3af4f3e47ef38cca206bf1ece47414e62bfe5669e4769f15376884d30c98',
        'original production dependency missing from actual compiled inputs')
    prepare.BASE = BASE
    prepare.main()


if __name__ == '__main__':
    main()
