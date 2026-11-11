"""Bounded numerical and project retrieval check under one strict GPU lease."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = [('docs/ARCHITECTURE.md', 32, 45),
             ('docs/ARCHITECTURE.md', 133, 140),
             ('docs/EVALUATION.md', 8, 14),
             ('docs/SSD_OFFLOAD_CONTRACT.md', 8, 20),
             ('docs/SERVE_NATIVE_OBSERVATION_2026-11-09.md', 25, 32)]
QUERIES = [
    'PLE 查到的数据如何从 SSD 送到模型层？',
    '是否必须为 q4t 安装或打包 nginx 才能提供服务？',
    '不涉及性能的功能修复需要运行完整性能矩阵吗？',
    '把专家权重从 GPU 移到 CPU 内存，能够减少 Thor 的物理 RAM 驻留吗？',
    '原生观测采集器会对模型发送停止或重启命令吗？']
INSTRUCTION = ('Instruct: Given a question about this project, retrieve the '
               'documentation passage that answers the question.\nQuery: ')
LIMITS = dict(seconds=900, cpu_model_starts=1, gpu_model_starts=1,
              native_forwards=14, reference_forwards=14, http_requests=0)


def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while part := stream.read(1024 * 1024):
            value.update(part)
    return value.hexdigest()


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write('\n')


def reference_interpreter(path):
    # Resolving a venv's python symlink selects /usr/bin/python instead and
    # silently drops its site-packages. Preserve the executable entry path.
    return str(path.absolute())


def prepare(args):
    from tokenizers import Tokenizer
    args.output.mkdir(parents=True, exist_ok=False)
    tokenizer = Tokenizer.from_file(str(args.assets / 'tokenizer.json'))
    cases = [dict(name=f'numerical-{length}', ids=[0] * (length - 1) + [151643],
                  trace_layers=True) for length in [1, 2, 17, 129]]
    sources = []
    for index, (path, first, last) in enumerate(DOCUMENTS):
        content = (ROOT / path).read_text()
        text = '\n'.join(content.splitlines()[first - 1:last])
        assert text
        sources.append(dict(path=path, first=first, last=last,
                            sha256=sha(ROOT / path), excerpt=text))
        cases.append(dict(name=f'document-{index}', text=text,
                          ids=tokenizer.encode(text).ids))
    for index, text in enumerate(QUERIES):
        text = INSTRUCTION + text
        cases.append(dict(name=f'query-{index}', text=text,
                          ids=tokenizer.encode(text).ids))
    assert len(cases) == LIMITS['native_forwards']
    identities = {path: sha(ROOT / path) for path in [
        'tools/native_embedding/model_replay.cpp',
        'tools/native_embedding/reference.py',
        'tools/native_embedding/acceptance.py',
        'include/q4t/model/embedding_model.h',
        'src/model/embedding_model.cu', 'src/model/embedding_layout.cpp',
        'src/server/embedding_assets.cpp', 'src/server/grammar_process.cpp',
        'src/text/tokenizer.cpp']}
    save(args.output / 'plan.json', dict(
        limits=LIMITS, binary=str(args.binary.resolve()),
        binary_sha256=sha(args.binary), assets=str(args.assets.resolve()),
        sources=identities, documents=sources, cases=cases,
        thresholds=dict(min_cosine=0.999, max_normalized_absolute_error=0.005,
                        max_relative_l2_error=0.03, retrieval_top1_required=5),
        instruction_is_explicit_input=True,
        default={str(path): sha(ROOT / path) for path in [
            'build/q4t', 'build/q4t.release.json']}))


def compare(output, plan):
    import numpy as np
    thresholds = plan['thresholds']
    def read(path):
        raw = np.fromfile(path, dtype='<u2')
        assert len(raw) and len(raw) % 1024 == 0
        values = (raw.astype('<u4') << 16).view('<f4').reshape(-1, 1024)
        assert np.isfinite(values).all()
        length = np.linalg.norm(values.astype('float64'), axis=1, keepdims=True)
        assert (length > 0).all()
        return values.astype('float64'), values / length
    results, final = [], []
    for index, case in enumerate(plan['cases']):
        native, reference = output / 'native' / f'case-{index}', \
                            output / 'reference' / f'case-{index}'
        expected = ['last.bf16']
        if case.get('trace_layers'):
            expected += [f'layer-{layer:02d}.bf16' for layer in range(29)]
        for name in expected:
            raw_a, a = read(native / name)
            raw_b, b = read(reference / name)
            assert a.shape == b.shape
            cosine = float(np.einsum('ij,ij->i', a, b).min())
            maximum = float(np.max(np.abs(a - b)))
            relative = float((np.linalg.norm(raw_a - raw_b, axis=1) /
                              np.linalg.norm(raw_b, axis=1)).max())
            results.append(dict(case=index, tensor=name, min_cosine=cosine,
                                max_normalized_absolute_error=maximum,
                                max_relative_l2_error=relative,
                                accepted=cosine >= thresholds['min_cosine'] and
                                maximum <= thresholds['max_normalized_absolute_error'] and
                                relative <= thresholds['max_relative_l2_error'] and
                                (name != 'layer-00.bf16' or relative == 0)))
        final.append(read(native / 'last.bf16')[1][0])
    retrieval = []
    docs = np.stack(final[4:9])
    for index, query in enumerate(final[9:14]):
        scores = docs @ query
        order = np.argsort(-scores).tolist()
        retrieval.append(dict(query=QUERIES[index], expected_document=index,
                              scores=scores.tolist(), ranking=order,
                              top1_correct=order[0] == index))
    result = dict(numerical=results, retrieval=retrieval,
                  numerical_accepted=all(row['accepted'] for row in results),
                  top1_correct=sum(row['top1_correct'] for row in retrieval),
                  total_queries=5)
    result['accepted'] = result['numerical_accepted'] and result['top1_correct'] == 5
    save(output / 'comparison.json', result)
    assert result['accepted'], 'native numerical or retrieval criteria not met'


def run(args):
    output = args.output.resolve()
    plan = json.loads((output / 'plan.json').read_text())
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    os.fstat(lease)
    assert plan['limits'] == LIMITS and sha(plan['binary']) == plan['binary_sha256']
    for path, expected in plan['sources'].items():
        assert sha(ROOT / path) == expected, path
    for path, expected in plan['default'].items():
        assert sha(ROOT / path) == expected, path
    started = time.monotonic()
    record = dict(status='FAILED', commands=[], limits=LIMITS)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
                       HF_HUB_OFFLINE='1', HF_HOME=str(output / 'hf-cache'),
                       TORCH_HOME=str(output / 'torch-cache'))
    commands = [
        ('reference', [reference_interpreter(args.reference_python),
                       str(ROOT / 'tools/native_embedding/reference.py'),
                       '--assets', plan['assets'], '--plan', str(output / 'plan.json'),
                       '--output', str(output / 'reference')]),
        ('native', [plan['binary'], plan['assets'], str(output / 'plan.json'),
                    str(output / 'native')])]
    try:
        for name, command in commands:
            remaining = LIMITS['seconds'] - (time.monotonic() - started)
            assert remaining > 0, 'phase deadline'
            begin = time.monotonic()
            with (output / f'{name}.stdout').open('x') as stdout, \
                    (output / f'{name}.stderr').open('x') as stderr:
                result = subprocess.run(command, cwd=ROOT, env=environment,
                                        pass_fds=(lease,), stdout=stdout,
                                        stderr=stderr, timeout=remaining)
            record['commands'].append(dict(name=name, argv=command,
                exit_code=result.returncode, seconds=time.monotonic() - begin))
            save(output / f'{name}-command.json', record['commands'][-1])
            assert result.returncode == 0, name + ' failed; preserve first result'
        compare(output, plan)
        record['status'] = 'ACCEPTED'
    except BaseException as error:
        record['error'] = repr(error)
        raise
    finally:
        record['seconds'] = time.monotonic() - started
        record['default_unchanged'] = all(sha(ROOT / path) == expected
                                        for path, expected in plan['default'].items())
        save(output / 'result.json', record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        assert record['default_unchanged']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'run'])
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--assets', type=Path)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--reference-python', type=Path,
                        default=ROOT / '.q4t-work/refenv/bin/python')
    args = parser.parse_args()
    (prepare if args.mode == 'prepare' else run)(args)


if __name__ == '__main__':
    main()
