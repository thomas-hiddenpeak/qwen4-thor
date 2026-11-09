"""Publish accepted N07, exercise actual retained N05, reselect accepted N07."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
from tools.serve_service import package as p
from tools.serve_recovery import bundle

BASE = ROOT/'.q4t-work/serve-lifecycle-20261109'
RELEASE = BASE/'release-01'
OUT = BASE/'delivery-01'
DEFAULT = ROOT/'build'
STATE = DEFAULT/'service-native-state'


def business_hashes():
    return {str(f.relative_to(STATE)): p.sha(f) for f in STATE.rglob('*')
            if f.is_file() and (f.name == 'ledger.json' or
                               f.relative_to(STATE).parts[0].startswith('client-'))}


def main():
    p.require(not OUT.exists(), 'delivery output exists; inspect before resuming')
    result = p.read(RELEASE/'result.json')
    checked = p.read(BASE/'packaged-01/result.json')
    review = p.read(RELEASE/'control-review.json')
    package = Path(result['package']['package'])
    digest = result['package']['manifest_sha256']
    cli = package/'engine/q4t'
    old = result['rollback']
    p.require(result['status'] == 'PASS_N07_ACCEPTED_PACKAGE_SEALED' and
              checked['status'] == 'PASS_N07_ACCEPTED_PACKAGED_CLI' and
              checked['manifest_sha256'] == digest, 'acceptance gate differs')
    p.require(all(p.sha(ROOT/n) == h for n, h in review['files'].items()),
              'accepted control changed')
    p.require(all(p.sha(r['path']) == r['sha256'] for r in review['defaults']),
              'default identity changed')
    p.require(bundle.verify(package, digest)['version'] == 10, 'package differs')
    before = business_hashes()
    initial_state = {str(f.relative_to(STATE)): p.sha(f)
                     for f in STATE.rglob('*.json')}
    p.require(initial_state == p.read(BASE/'packaged-01/state-before-hashes.json'),
              'state changed after packaged checks')
    OUT.mkdir(mode=0o700)
    ids = {name: str(uuid.uuid4()) for name in ('publish', 'rollback', 'forward')}
    p.save(OUT/'plan.json', dict(version=1, source_commit=subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], text=True).strip(), package=str(package),
        manifest_sha256=digest, previous=old, request_ids=ids,
        operations=['publish revision 0', 'new default check/status/publication',
                    'retained N05 rollback revision 2', 'old selected check/status',
                    'forward publish revision 4', 'new default check/status/publication'],
        expected_final_revision=6, model_starts=0, chat_requests=0,
        automatic_start=False, business_state_before=before))
    decision = dict(status='PASS_N07_DEFAULT_DELIVERY_PREREQUISITES',
        acceptance='ACCEPT_TARGETED_NATIVE_LIFECYCLE',
        package=str(package), manifest_sha256=digest,
        source_commit=review['source_commit'],
        accepted_release=p.sha(RELEASE/'decision.json'),
        packaged_cli=p.sha(BASE/'packaged-01/result.json'),
        business=p.sha(ROOT/'docs/evidence/serve-lifecycle-business-20261109/decision.json'),
        publication_cpu=p.sha(ROOT/'docs/evidence/serve-lifecycle-publication-20261109/index.json'),
        default_capacity=review['default_capacity'],
        default_profile_evidence=review['actual_configuration_provenance'],
        installation_pending=True, performance_acceptance=False,
        full_model_quality=False, long_stability_accepted=False,
        remaining_master_tasks_open=True)
    p.save(OUT/'decision.json', decision)
    metadata = p.read(RELEASE/'previous/service.release.json')
    metadata.update(acceptance=decision['acceptance'], recovery_accepted=True,
        acceptance_record=str(OUT/'decision.json'),
        acceptance_sha256=p.sha(OUT/'decision.json'), package=str(package),
        manifest_sha256=digest, engine_binary_sha256=p.sha(package/'engine/q4t'),
        engine_release_sha256=p.sha(package/'engine/q4t.release.json'),
        entry=str(DEFAULT/'q4t')+' service',
        previous_acceptance_record=metadata['acceptance_record'],
        scope='Accepted native lifecycle, configurable capacity and persistent default publication. Numeric defaults and all mutable business state preserved; actual custom capacity business and prior numeric-default evidence are distinguished.',
        control_review=str(RELEASE/'control-review.json'),
        client_artifacts=str(RELEASE/'clients'),
        native_lifecycle_targeted_accepted=True, persistent_default_publication=True,
        default_capacity=review['default_capacity'],
        rollback_receipt=str(RELEASE/'rollback-receipt.json'),
        rollback_sha256=p.sha(RELEASE/'rollback-receipt.json'),
        limitations=[
            'Remaining OpenAI inference/application tasks stay active in the master plan.',
            'N06 gateway rejection needs a typed diagnostic without restarting a healthy model.',
            'R05/R06 validated downgrade and automatic older-code recovery remain required.',
            'NVML-free cold start, resource governance and whole-host recovery remain active.',
            'Long stability remains user-run; TLS/CORS/proxy options stay deployment-selected.'])
    p.save(OUT/'service.release.json', metadata)
    rows = []

    def run(label, argv):
        start = time.monotonic()
        value = subprocess.run([str(x) for x in argv], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=45)
        (OUT/(label+'.stdout')).write_bytes(value.stdout)
        (OUT/(label+'.stderr')).write_bytes(value.stderr)
        row = dict(label=label, argv=[str(x) for x in argv], exit_code=value.returncode,
                   seconds=time.monotonic()-start)
        rows.append(row)
        p.save(OUT/(label+'.json'), row)
        p.require(value.returncode == 0, 'delivery command failed: '+label)
        p.require(business_hashes() == before, 'business state changed: '+label)
        return json.loads(value.stdout)

    state_args = ['--state', STATE]

    def publish(label, expected, revision):
        value = run(label, [cli, 'service', 'publish', *state_args,
            '--default-root', DEFAULT, '--package', package,
            '--manifest-sha256', digest, '--service-release', OUT/'service.release.json',
            '--expected-manifest-sha256', expected, '--expected-revision', revision,
            '--request-id', ids[label]])
        p.require(value['phase'] == 'stable' and value['durable'] and
                  value['revision'] == revision+2 and
                  value['current']['manifest_sha256'] == digest, 'publish receipt differs')
        return value

    def verify_default(label, executable, target, expected, revision):
        for name, source in [('q4t', target/'engine/q4t'),
                             ('q4t.release.json', target/'engine/q4t.release.json')]:
            p.require((DEFAULT/name).is_symlink() and p.sha(DEFAULT/name) == p.sha(source),
                      'default artifact identity differs')
        p.require((DEFAULT/'service').resolve() == target, 'default package differs')
        args = [executable, 'service']
        selected = [*state_args, '--package', target, '--manifest-sha256', expected]
        check = run(label+'-check', [*args, 'check', *selected])
        p.require(check['status'] == 'PASS' and check['desired'] == 'STOPPED' and
                  not check['active_grants'] and check['client_state_valid'], 'default check failed')
        status = run(label+'-status', [*args, 'status', *selected])
        p.require(status['state'] == 'STOPPED' and status['cleanup_confirmed'], 'default not stopped')
        pub = run(label+'-publication', [*args, 'publish-status', *state_args])
        p.require(pub['status'] == 'PASS' and pub['ready'] and pub['revision'] == revision and
                  pub['current']['manifest_sha256'] == expected, 'publication not committed')
        return pub

    try:
        first = publish('publish', old['manifest_sha256'], 0)
        verify_default('installed', DEFAULT/'q4t', package, digest, 2)
        retained = first['history'][-1]['previous']
        rollback = run('rollback', [cli, 'service', 'publish-rollback', *state_args,
            '--default-root', DEFAULT, '--generation-sha256', retained['generation_sha256'],
            '--expected-manifest-sha256', digest, '--expected-revision', 2,
            '--request-id', ids['rollback']])
        p.require(rollback['phase'] == 'stable' and rollback['revision'] == 4 and
                  rollback['current'] == retained, 'rollback receipt differs')
        p.require(p.sha(DEFAULT/'service.release.json') == old['service_release_sha256'],
                  'old service metadata differs')
        verify_default('rolled-back', cli, Path(old['package']), old['manifest_sha256'], 4)
        publish('forward', old['manifest_sha256'], 4)
        final = verify_default('final', DEFAULT/'q4t', package, digest, 6)
        p.require(p.sha(DEFAULT/'service.release.json') == p.sha(OUT/'service.release.json'),
                  'new service metadata differs')
        p.save(OUT/'result.json', dict(status='PASS_N07_DEFAULT_DELIVERY', commands=rows,
            current=final['current'], retained_previous=retained,
            final_publication_revision=6, service_id=metadata['service_id'],
            state='STOPPED', business_state_preserved=True,
            business_state_after=business_hashes(), actual_old_generation_rollback=True,
            model_starts=0, chat_requests=0, gpu_queries=0, automatic_start=False,
            main_merged=False, remaining_master_tasks_open=True))
    except BaseException as error:
        p.save(OUT/'result.json', dict(status='FAILED', commands=rows,
            category=type(error).__name__, detail=str(error),
            business_state_preserved=business_hashes() == before,
            model_starts=0, chat_requests=0, automatic_start=False,
            recovery='Inspect the saved immutable request and use publish-resume. Never restore old mutable state.'))
        raise
    print(json.dumps(dict(status='PASS_N07_DEFAULT_DELIVERY', commands=len(rows),
        revision=6, package=digest, old_generation=retained['generation_sha256'])))


if __name__ == '__main__':
    os.umask(0o077)
    main()
