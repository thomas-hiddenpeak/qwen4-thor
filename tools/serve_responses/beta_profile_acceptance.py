"""Qualify the Responses Beta selector on both deployed recovery capacities."""
import argparse
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import profile_acceptance
from tools.serve_recovery import bundle
from tools.serve_service import package as pkg


def protocol(sources):
    data = ''.join(f'{name}:{digest}\n' for name, digest in sorted(sources.items()))
    return hashlib.sha256(data.encode()).hexdigest()


def prepare(output, build, sdk_stage):
    handoff = pkg.read(ROOT/'docs/evidence/serve-live-stability-handoff-20261123/handoff.json')
    source = ROOT/'build/service'
    digest = handoff['publication']['primary']
    manifest = bundle.verify(source, digest)
    selected = pkg.verify(source/'engine', manifest['engine_manifest_sha256'])
    metadata = pkg.read(source/'engine/q4t.release.json')
    original = Path(metadata['tested_source_manifest'])
    pkg.require(pkg.sha(original) == metadata['tested_source_manifest_sha256'],
                'installed runtime source manifest differs')
    previous = pkg.read(original)
    accepted = pkg.read(ROOT/'docs/evidence/serve-responses-beta-selector-20261123/checks.json')
    for name in ('final-01/plan.json', 'final-01/result.json', 'final-01/sdk-result.json',
                 'final-lease-01/exit.json'):
        pkg.require(pkg.sha(sdk_stage/name) == accepted['sha256'][name],
                    'retained Beta SDK business differs')
    sdk = pkg.read(sdk_stage/'final-01/plan.json')
    first = pkg.read(sdk_stage/'profile-input/compiled.json')
    compiled = pkg.read(build/'generated/q4t/server/chat_protocol_sources.json')
    pkg.require(set(first) == set(compiled) and first.keys() <= previous.keys() and
                protocol({n: previous[n] for n in first}) == selected['protocol_sha256'] and
                {n for n in first if first[n] != previous[n]} == {
                    'include/q4t/server/responses_api.h', 'src/server/chat_http.cpp',
                    'src/server/responses_api.cpp'} and
                {n for n in compiled if compiled[n] != first[n]} ==
                {'src/server/chat_capabilities.cpp'} and
                all(pkg.sha(ROOT/n) == h for n, h in compiled.items()),
                'unexpected change between installed, SDK and declaration builds')
    pkg.require(sdk['binary'] == accepted['elf_sha256'] == pkg.sha(sdk_stage/'profile-input/q4t')
                and sdk['ingress'] == manifest['files']['bin/q4t-ingress']
                and sdk['protected']['build/service/manifest.json'] == digest,
                'SDK engine, ingress or installed package differs')
    protected = {str(ROOT/'build'/name): pkg.sha(ROOT/'build'/name) for name in (
        'q4t', 'q4t.release.json', 'service/manifest.json', 'service.release.json',
        'service-native-state/ledger.json')}
    workers = dict(selected['live_workers'], engine_sha256=pkg.sha(build/'q4t'))
    pkg.require(protected[str(source/'manifest.json')] == digest and
                selected['live_workers']['engine_sha256'] == metadata['binary_sha256'],
                'installed actors changed during preparation')
    change = dict(source_manifest_sha256=digest, previous_protocol=selected['protocol_sha256'],
                  sdk_binary=sdk['binary'], sdk_protocol=protocol(first), protocol=protocol(compiled))
    profile_acceptance.prepare_profiles(output, manifest, selected, workers, sdk['ingress'],
                                        beta_change=change, protected=protected)


def verify(phase, plan, capabilities):
    pkg.require(capabilities['responses']['beta_query_selector'] == 'beta=true' and
                capabilities['protocol_source_sha256'] == plan['beta_change']['protocol'],
                'deployed Beta selector declaration differs')
    count = pkg.read(phase/'beta-count.json')
    output = pkg.read(phase/'beta-response.json')
    pkg.require(count.get('input_tokens', 0) > 0 and output.get('status') == 'completed' and
                output['usage']['input_tokens'] == count['input_tokens'] and
                ''.join(c['text'] for item in output['output'] for c in item.get('content', [])
                        if c['type'] == 'output_text').strip() == '42', 'Beta model response')
    files = ['beta-response.json', 'beta-count.json']
    if plan['name'] == 'light':
        prior = pkg.read(phase.parent/'primary/beta-response.json')
        receipt = pkg.read(phase/'beta-restored.json')
        restored = receipt['response']
        pkg.require(restored['id'] == prior['id'] and restored['usage'] == prior['usage'] and
                    receipt['other_status'] == 404, 'Beta history after capacity change')
        files.append('beta-restored.json')
    return {name: pkg.sha(phase/name) for name in files}


def business(stage, capabilities):
    prompt = dict(model='qwen3.8-flash-next', reasoning=dict(effort='none'),
                  input='Compute 17 + 25. Reply only with the decimal number.')
    status, count = stage.api('POST', '/v1/responses/input_tokens?beta=true', body=prompt)
    pkg.require(status == 200 and count.get('input_tokens', 0) > 0, 'Beta token count')
    status, output = stage.api('POST', '/v1/responses?beta=true',
        body=dict(prompt, store=True, temperature=0, max_output_tokens=32))
    pkg.require(status == 200, 'Beta model HTTP status')
    pkg.save(stage.out/'beta-response.json', output)
    pkg.save(stage.out/'beta-count.json', count)
    if stage.plan['name'] == 'light':
        prior = pkg.read(stage.out.parent/'primary/beta-response.json')
        path = '/v1/responses/' + prior['id'] + '?beta=true'
        own, restored = stage.api('GET', path)
        other, _ = stage.api('GET', path, client='gamma')
        pkg.require(own == 200, 'Beta history HTTP status')
        pkg.save(stage.out/'beta-restored.json', dict(response=restored, other_status=other))
    verify(stage.out, stage.plan, capabilities)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--sdk-stage', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    pkg.require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory')
    prepare(out, args.build.resolve(), args.sdk_stage.resolve())
