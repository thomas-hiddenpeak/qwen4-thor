"""Unprivileged verification of an existing outer intent for a root issuer.

Only installed protected source is executed by the root authority. Reading a
service's mutable package and journals always happens after permanent UID drop.
This verifier neither creates an intent nor changes any core or outer budget.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from tools.serve_recovery import child, cold_requests, deployment, host_entry, host_install
from tools.serve_recovery import ledger, lifecycle, outer_manager, outer_store, store


def verify(directory, selected, lease_fd, instance_fd):
    cold_requests.request(selected)
    host_entry.no_new_privileges(); checked = host_entry.check(directory)
    host = host_install.public(directory); state = Path(host['state'])
    ledger.require(host['enabled'] and host['phase'] == 'ready' and checked['outer_enabled'] and
        host['installation_id'] == selected['host_id'] and host['service_id'] == selected['service_id'] and
        host['artifact']['manifest_sha256'] == selected['manifest_sha256'],
        'cold runtime host selection changed')
    with ExitStack() as locks:
        locks.enter_context(deployment.claim(state, shared=True))
        chosen = host['artifact']
        desc = lifecycle.selected_state(state, chosen['package'], chosen['manifest_sha256'])
        binding = outer_manager.ready(state, desc)
        outer = locks.enter_context(outer_store.open_bound(binding))
        core = locks.enter_context(store.Store(state, desc['service_id']))
        policy = outer.head['state']['policy']; attempt = policy['pending']
        ledger.require(policy['enabled'] and not policy['protected'] and attempt is not None and
            all(attempt[n] == selected[n] for n in
                ('ticket', 'serial', 'boot_id', 'manifest_sha256', 'custody')) and
            attempt['owner'] is None and attempt['cleanup'] is None and 'resource' in attempt,
            'cold request lacks the original unstarted outer intent')
        ledger.require(core.service_contract and core.state['desired'] == 'RUNNING' and
            core.state['quarantine'] is None and not any(core.state['active'].values()) and
            ledger.digest(core.state) == attempt['core_sha256'],
            'cold request core stop, fault, slots or intent changed')
        manifest = host_install.artifact.read(Path(chosen['package'])/'manifest.json',
            host_install.artifact.MANIFEST_LIMIT, chosen['manifest_sha256'])
        manifest = store.decode(manifest)
        child.verify_lease(lease_fd, manifest['lock_path'])
        child.verify_lease(instance_fd, str(state/'guardian.lock'))
        held = os.fstat(lease_fd)
        ledger.require((held.st_dev, held.st_ino) == (selected['lease_device'], selected['lease_inode']),
                       'cold request lease changed')
        publication = attempt['resource']['publication']
        ledger.require(attempt['resource']['directory'] == host['resource']['directory'] and
            publication['domain_id'] == host['resource']['domain_id'] and
            publication['record']['policy'] == host['resource']['policy'],
            'cold outer resource selection changed')
        return dict(verified=True, request_sha256=ledger.digest(selected),
            core_sha256=ledger.digest(core.state), resource=attempt['resource'],
            lease_path=manifest['lock_path'], model_actions=False)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--lease-fd', type=int, required=True)
    parser.add_argument('--instance-fd', type=int, required=True)
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(16385)
    ledger.require(0 < len(raw) <= 16384, 'cold verifier input bound')
    value = verify(args.directory, store.decode(raw), args.lease_fd, args.instance_fd)
    print(json.dumps(value, sort_keys=True))


if __name__ == '__main__': main()
