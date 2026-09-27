"""Direct format regression on frozen headers; never changes runtime sources."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[3]
P = ROOT / '.q4t-work/prepared/e4m3-site-isolation-20260927'
OUT = ROOT / '.q4t-work/e2e/e4m3-contract-regression-20260927'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def main():
    OUT.mkdir()
    shutil.copy2(__file__, OUT / 'run_contract.py')
    tool = ROOT / 'tools/verify/verify_e4m3_contract.cu'
    old = ROOT / '.q4t-work/prepared/e4m3-rounding-20260923'
    source = old / 'format-rejected.h'
    assert sha(source) == json.loads((old / 'build-result.json').read_text())['source_sha256']
    headers = {'baseline': P / 'format.h',
               'high_only': P / 'input/format.h', 'full_fix': source}
    expected_sha = {'baseline': json.loads((P / 'plan.json').read_text())['sources']['include/q4t/quant/format.h'],
                    'high_only': json.loads((P / 'input/build-result.json').read_text())['source_sha256']['include/q4t/quant/format.h'],
                    'full_fix': sha(source)}
    records = []
    for kind, header in headers.items():
        assert sha(header) == expected_sha[kind]
        d = OUT / kind
        include = d / 'include/q4t/quant'
        include.mkdir(parents=True)
        shutil.copy2(header, include / 'format.h')
        code = tool.read_text()
        if kind == 'high_only':
            assert code.count('FloatToE4m3(') == 2
            code = code.replace('FloatToE4m3(', 'FloatToE4m3<true>(')
        (d / 'verify.cu').write_text(code)
        command = ['nvcc', '-std=c++23', '-arch=sm_110a', '-ccbin=g++-14',
                   '-Xcompiler=-Wall,-Wextra', '-I' + str(d / 'include'),
                   str(d / 'verify.cu'), '-o', str(d / 'verify')]
        save(d / 'build-command.json', command)
        with (d / 'build.log').open('w') as log:
            code = subprocess.run(command, stdout=log,
                                  stderr=subprocess.STDOUT).returncode
        assert code == 0 and (d / 'build.log').stat().st_size == 0
        with (d / 'run.log').open('w') as log:
            code = subprocess.run([str(d / 'verify'), str(d / 'cases.tsv')],
                                  stdout=log, stderr=subprocess.STDOUT).returncode
        assert code in (0, 1)
        rows = []
        for line in (d / 'cases.tsv').read_text().splitlines():
            value, expected, host, device, native = line.split()
            rows.append((float.fromhex(value), *map(int, [expected, host, device, native])))
        assert len(rows) == 33146
        counts = {'host': sum(x[2] != x[1] for x in rows),
                  'device': sum(x[3] != x[1] for x in rows),
                  'native': sum(x[4] != x[1] for x in rows),
                  'host_device': sum(x[2] != x[3] for x in rows),
                  'high_region': sum(x[3] != x[1] and x[0] >= 0.015625 for x in rows),
                  'subnormal_region': sum(x[3] != x[1] and x[0] < 0.015625 for x in rows)}
        record = {'kind': kind, 'source': str(header.relative_to(ROOT)),
                  'source_sha256': sha(header), 'tool_source_sha256': sha(tool),
                  'tested_source_sha256': sha(d / 'verify.cu'), 'exit': code,
                  'records': len(rows), 'mismatches': counts}
        save(d / 'result.json', record)
        records.append(record)
        print(record, flush=True)
    save(OUT / 'summary.json', {'records': records, 'runtime_changed': False,
         'full_fix_runtime_accepted': False,
         'scope': 'Nonnegative finite values: exact E4M3, midpoint/adjacent FP32, all finite BF16, FLT_MAX. Not all FP32, NaN, Inf or negative inputs. Full fix is the previously rejected archived implementation, not a new runtime candidate.'})


if __name__ == '__main__':
    main()
