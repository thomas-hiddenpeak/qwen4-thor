"""Seal the accepted Live observations and their retained business history."""
import argparse
import copy
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import profile_qualification
from tools.serve_recovery import bundle, ledger, profile_bundle, profile_contract
from tools.serve_recovery import profile_live_reuse, profile_manager, profile_migration, profile_record
from tools.serve_service import package


def prepare(source, manifest_sha256, stage, observations, output):
    output = output.resolve()
    package.require(output.is_relative_to(ROOT/'.q4t-work') and not output.exists(),
                    'Live registry needs an unused private output')
    source = source.resolve(strict=True)
    manifest = bundle.verify(source, manifest_sha256)
    engine = package.verify(source/'engine', manifest['engine_manifest_sha256'])
    accepted = package.read(stage/'acceptance.json')
    actual = dict(profile_qualification.observations(stage, accepted),
                  acceptance_sha256=package.sha(stage/'acceptance.json'))
    package.require(package.same_json(package.read(observations), actual),
                    'reviewed Live observations differ from the retained run')
    previous = manifest['recovery_profiles']
    registry = copy.deepcopy(previous)
    identity = registry['identity']
    identity.update(binary_sha256=actual['engine_sha256'],
                    protocol_sha256=actual['profiles']['primary']['protocol_sha256'],
                    model_assets=bundle.observed_assets(engine['model_dir'], manifest))
    environment = dict(accepted['environment'],
                       lease_result_sha256=package.sha(stage/'lease/exit.json'))
    evidence = {}
    prior = profile_manager.evidence(source, manifest)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='.live-registry-') as temp:
        work = Path(temp)
        qualified = work/'qualified'
        qualified.mkdir(mode=0o700)
        for name in profile_contract.NAMES:
            old = prior[name]['record']
            item = actual['profiles'][name]
            package.require(registry['profiles'][name] == item['capacity'],
                            'Live observations changed the selected capacities')
            row = copy.deepcopy(old)
            capabilities = package.read(stage/name/'capabilities.json')
            row.update(format=profile_live_reuse.format_for(capabilities), identity=identity,
                       capabilities=capabilities,
                       configuration=item['configuration'], startup_memory=item['startup_memory'],
                       environment=environment)
            row['business']['live'] = dict(observation=accepted['profiles'][name],
                workers=item['workers'], evidence_sha256=item['evidence_sha256'],
                acceptance_sha256=actual['acceptance_sha256'])
            profile_live_reuse.attach(row, old)
            path = qualified/(name+'.json')
            profile_record.save(path, row)
            digest = package.sha(path)
            registry['qualifications'][name] = profile_contract.qualification(
                name, item['capacity'], identity, row['capabilities'],
                item['configuration'], item['startup_memory'], digest)
            evidence[name] = dict(record=row, sha256=digest)
        for name in profile_contract.NAMES:
            profile_bundle.verify_record(qualified/(name+'.json'), registry, name,
                                         dict(live_workers=actual['profiles'][name]['workers']),
                                         ingress_sha256=actual['ingress_sha256'])
        config = manifest['recovery_profile_policy']
        permit = profile_migration.permit(previous, registry, prior, evidence, config, config,
            allow_capability_extension=True, reviewed_content=manifest.get('model_ctime_binding'))
        package.save(qualified/'registry.json', registry)
        package.save(work/'plan.json', dict(previous=dict(package=str(source),
            manifest_sha256=manifest_sha256), observations_sha256=package.sha(observations),
            acceptance_sha256=actual['acceptance_sha256'], engine_sha256=actual['engine_sha256'],
            ingress_sha256=actual['ingress_sha256']))
        status = {profile_live_reuse.RECORD_FORMAT: 'ACCEPT_LIVE_PRIMARY_LIGHT',
                  profile_live_reuse.BETA_RECORD_FORMAT: 'ACCEPT_RESPONSES_BETA_PRIMARY_LIGHT',
                  profile_live_reuse.AGENT_RECORD_FORMAT: 'ACCEPT_HOSTED_AGENTS_PRIMARY_LIGHT',
                  profile_live_reuse.REGISTRATION_RECORD: 'ACCEPT_AGENT_REGISTRATION_PRIMARY_LIGHT',
                  profile_live_reuse.SESSION_RECORD: 'ACCEPT_AGENT_SESSIONS_PRIMARY_LIGHT'}
        primary = evidence['primary']['record']
        label = status[primary['format']]
        if primary['reused_business']['format'] == profile_live_reuse.AGENT_REFRESH:
            label = 'ACCEPT_AGENT_RECOVERY_PRIMARY_LIGHT'
        package.save(work/'acceptance.json', dict(status=label,
            registry_sha256=package.sha(qualified/'registry.json'), environment=environment,
            migration_permit_sha256=ledger.digest(permit), actual_model_starts=2,
            additional_model_starts=0, historical_business_preserved=True))
        os.rename(work, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--observations', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.package, args.manifest_sha256, args.stage, args.observations, args.output)


if __name__ == '__main__':
    main()
