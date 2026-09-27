"""Reproduce the known defect and archived repair without changing the engine."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OLD_SHA = '24c372233cebbb73ca88ee9b69e8937c4bf97cf2dbb096975eb85132eed24247'
FIX_SHA = '4f8e654989a53ca32e31b5a37a502444dcee67936fa5159b3b28b836fc84f11d'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-header', type=Path,
                        default=ROOT / 'include/q4t/quant/format.h')
    args = parser.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / x) for x in ['build', '.q4t-work'])
    assert sha(args.baseline_header) == OLD_SHA, 'Use the recorded baseline header'
    out.mkdir(parents=True, exist_ok=False)
    tool = ROOT / 'tools/verify/verify_e4m3_contract.cu'
    patch = Path(__file__).with_name('full-format-fix.patch')
    shutil.copy2(__file__, out / 'reproduce_format.py')
    shutil.copy2(patch, out / patch.name)
    records = []
    for kind, wanted_sha in [('baseline', OLD_SHA), ('fixed', FIX_SHA)]:
        d = out / kind
        header = d / 'include/q4t/quant/format.h'
        header.parent.mkdir(parents=True)
        shutil.copy2(args.baseline_header, header)
        if kind == 'fixed':
            with (d / 'patch.log').open('w') as log:
                subprocess.run(['patch', '--batch', '-p1', '-i', str(patch)],
                               cwd=d, stdout=log, stderr=subprocess.STDOUT,
                               check=True)
        assert sha(header) == wanted_sha
        shutil.copy2(tool, d / 'verify.cu')
        command = ['nvcc', '-std=c++23', '-arch=sm_110a', '-ccbin=g++-14',
                   '-Xcompiler=-Wall,-Wextra', '-I' + str(d / 'include'),
                   str(d / 'verify.cu'), '-o', str(d / 'verify')]
        (d / 'build-command.json').write_text(json.dumps(command, indent=2) + '\n')
        with (d / 'build.log').open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        assert (d / 'build.log').stat().st_size == 0
        with (d / 'run.log').open('w') as log:
            code = subprocess.run([str(d / 'verify'), str(d / 'cases.tsv')],
                                  stdout=log, stderr=subprocess.STDOUT).returncode
        rows = [line.split() for line in (d / 'cases.tsv').read_text().splitlines()]
        assert len(rows) == 33146
        counts = {name: sum(r[i] != r[1] for r in rows)
                  for name, i in [('host', 2), ('device', 3), ('native', 4)]}
        expected = {'host': 147, 'device': 147, 'native': 0} if kind == 'baseline' else dict.fromkeys(counts, 0)
        assert code == (1 if kind == 'baseline' else 0) and counts == expected
        record = {'kind': kind, 'exit': code, 'cases': len(rows),
                  'mismatches': counts, 'header_sha256': sha(header),
                  'scope': 'Finite nonnegative samples; not exhaustive FP32 or model quality'}
        records.append(record)
        print(record, flush=True)
    (out / 'summary.json').write_text(json.dumps({
        'records': records, 'runtime_changed': False,
        'baseline_contract_passed': False, 'fixed_contract_passed': True,
        'runtime_fix_accepted': False}, indent=2) + '\n')


if __name__ == '__main__':
    main()
