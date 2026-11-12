"""Actual CPU tree/journal retirement; synthetic role grants never run a GPU."""
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import access_store, deployment, journal, ledger
from tools.serve_recovery import outer_instance, outer_policy, outer_reconcile
from tools.serve_recovery import outer_store, outer_tree, store


def transact(writer, operation, **args):
    return writer.transact(dict(id=str(uuid.uuid4()), expected_revision=writer.state['revision'],
                                operation=operation, args=args))['result']


def read_core(path, sid):
    with store.Store(path, sid) as writer: return deepcopy(writer.state)


def read_outer(binding):
    with outer_store.open_bound(binding) as writer: return deepcopy(writer.head['state']['policy'])


def cycle(writer, owner, *, leave=False):
    for role in ledger.ROLES:
        result = transact(writer, 'authorize', role=role, proof={k:True for k in ledger.PROOFS})
        assert result['allowed'], result
        # An authorized-but-unrecorded gateway exercises the lost spawn window.
        if role != 'gateway' or not leave:
            transact(writer, 'spawned', role=role, ticket=result['grant']['ticket'], owner=owner)
    if not leave:
        for role in ('model','controller','gateway'):
            transact(writer,'retired',role=role,ticket=writer.state['active'][role]['ticket'],verified=True)


def worker(path):
    desc = deployment.read_descriptor(path)
    child = os.fork()
    if child == 0:
        os.setsid(); signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (path/'orphan').write_text(str(os.getpid()))
        time.sleep(10); os._exit(0)
    now = store.stamp()
    owner = dict(pid=os.getpid(),start_ticks=journal.process_start(os.getpid()),boot_id=now.boot_id)
    with store.Store(path,desc['service_id']) as writer:
        cycle(writer,owner,leave=True)
        transact(writer,'fault',generation=writer.state['generations']['model'],fingerprint='f'*64,
                 unknown=False)
        transact(writer,'quarantine',reason='device_failure')
        transact(writer,'desired',state='STOPPED')
    while not (path/'orphan').exists(): time.sleep(.002)
    (path/'ready').touch()
    os._exit(17)


def until(predicate):
    end = time.monotonic()+3
    while not predicate():
        if time.monotonic() >= end: raise TimeoutError('CPU reconciliation fixture readiness')
        time.sleep(.005)


def scenario(name, path):
    sid = str(uuid.uuid4()); digest = 'a'*64
    desc = dict(version=1,service_id=sid,package=str(path/'sealed'),manifest_sha256=digest)
    deployment.write_descriptor(path,desc)
    with store.Store(path,sid,initialize=True) as writer:
        writer.require_service_contract()
        transact(writer,'desired',state='RUNNING')
        cycle(writer,dict(pid=os.getpid(),start_ticks=journal.process_start(os.getpid()),
                          boot_id=store.stamp().boot_id))
    binding = access_store.binding(path,sid)
    outer_store.initialize_bound(binding,outer_policy.initial(sid,store.stamp()))
    outer_store.commit_once(binding,'configure',dict(enabled=True))
    with outer_instance.Instance(path,sid) as instance, outer_tree.Tree() as tree:
        attempt = outer_store.commit_once(binding,'request',dict(core=read_core(path,sid),
            ticket=str(uuid.uuid4()),manifest_sha256=digest,cleanup_verified=True,identity_valid=True,
            custody=tree.binding(instance)))['state']['pending']
        owner = tree.launch([sys.executable,'-I','-B',__file__,'--worker',str(path)],env={},
                            instance=instance,attempt=attempt)
        try:
            if name != 'lost_spawn':
                outer_store.commit_once(binding,'spawned',dict(ticket=attempt['ticket'],owner=owner))
            until(lambda:(path/'ready').exists() and tree.poll() is not None)
            before = read_core(path,sid); outer_before = read_outer(binding)
            try: outer_reconcile.continuous(instance,tree); raise AssertionError('live orphan retired')
            except ValueError: pass
            assert read_core(path,sid) == before and read_outer(binding) == outer_before
            tree.drain(term_seconds=.02,kill_seconds=1)
            if name == 'reacquired':
                instance.close(); instance.__enter__()
                try: outer_reconcile.continuous(instance,tree);raise AssertionError('reacquired lock accepted')
                except ValueError: pass
                assert read_core(path,sid) == before and read_outer(binding) == outer_before
                return dict(reacquired_refused=True)
            if name == 'new_tree':
                with outer_tree.Tree() as other:
                    try:
                        other.launch(['/bin/true'],env={},instance=instance,attempt=attempt)
                        raise AssertionError('old receipt accepted by new tree')
                    except ValueError: pass
                    assert other.root is None
                assert read_core(path,sid) == before and read_outer(binding) == outer_before
                return dict(new_tree_refused=True)
            if name == 'descriptor':
                deployment.write_descriptor(path,dict(desc,manifest_sha256='b'*64))
                try: outer_reconcile.continuous(instance,tree);raise AssertionError('changed release accepted')
                except ValueError: pass
                assert read_core(path,sid) == before and read_outer(binding) == outer_before
                return dict(descriptor_refused=True)
            if name == 'core_interrupt':
                original = os.replace
                def fail(src,dst,**kwargs):
                    if src == store.PENDING: raise OSError('injected core pre-rename failure')
                    return original(src,dst,**kwargs)
                with mock.patch.object(os,'replace',side_effect=fail):
                    try: outer_reconcile.continuous(instance,tree);raise AssertionError('missing fault')
                    except OSError: pass
                assert (path/store.PENDING).exists()
                assert read_outer(binding)['pending'] == outer_before['pending']
            if name == 'outer_lost':
                original = outer_store.Store.transact
                def lost(writer,*args,**kwargs):
                    original(writer,*args,**kwargs)
                    raise OSError('injected lost durable outer retirement receipt')
                with mock.patch.object(outer_store.Store,'transact',new=lost):
                    try: outer_reconcile.continuous(instance,tree);raise AssertionError('missing receipt fault')
                    except OSError: pass
                assert not any(read_core(path,sid)['active'].values())
                assert read_outer(binding)['pending'] is None
            result = outer_reconcile.continuous(instance,tree)
            after = read_core(path,sid); outer_after = read_outer(binding)
            assert all(after[k] == before[k] for k in ('desired','quarantine','generations',
                'attempts','not_before','circuit_until','failures'))
            assert after['desired'] == 'STOPPED' and after['quarantine'] == 'device_failure'
            assert after['revision'] == before['revision']+3
            assert all(after['attempts'][role] for role in ledger.ROLES)
            assert not any(after['active'].values()) and outer_after['pending'] is None
            assert outer_after['total'] == outer_before['total'] == 1
            assert outer_after['starts'] == outer_before['starts']
            assert result['already_retired'] == (name == 'outer_lost')
            assert not result['device_authorized'] and not result['model_actions']
            retry = outer_reconcile.continuous(instance,tree)
            assert retry['already_retired'] and read_core(path,sid) == after
            assert read_outer(binding) == outer_after
            return dict(protection_preserved=True, spent_attempts_preserved=True,
                        actual_tree_reaped=True, result=result)
        finally:
            tree.drain(term_seconds=.02,kill_seconds=1)


class ReconcileTests(unittest.TestCase):
    def run_case(self,name):
        base=ROOT/'.q4t-work/serve-outer-recovery-20261112/reconcile-fixtures'
        base.mkdir(mode=0o700,parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            result=subprocess.run([sys.executable,'-I','-B',__file__,'--case',name,temporary],
                                  capture_output=True,text=True,timeout=6)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)

    def test_actual_orphan_retirement_preserves_stop_faults_and_budgets(self):
        value=self.run_case('normal')
        self.assertTrue(value['protection_preserved'])
        self.assertEqual([row['role'] for row in value['result']['retired']],
                         ['model','controller','gateway'])

    def test_lost_spawn_receipt_requires_original_live_custody(self):
        self.assertTrue(self.run_case('lost_spawn')['actual_tree_reaped'])

    def test_released_then_reacquired_instance_cannot_retire_old_slots(self):
        self.assertTrue(self.run_case('reacquired')['reacquired_refused'])

    def test_new_tree_cannot_redispatch_original_durable_attempt(self):
        self.assertTrue(self.run_case('new_tree')['new_tree_refused'])

    def test_changed_deployment_cannot_consume_old_tree_proof(self):
        self.assertTrue(self.run_case('descriptor')['descriptor_refused'])

    def test_interrupted_core_retirement_resumes_without_reset_or_double_charge(self):
        self.assertTrue(self.run_case('core_interrupt')['spent_attempts_preserved'])

    def test_lost_outer_retirement_receipt_is_idempotent_with_same_custody(self):
        self.assertTrue(self.run_case('outer_lost')['result']['already_retired'])

    def test_regressed_core_old_generation_and_foreign_boot_never_retire(self):
        sid=str(uuid.uuid4());now=store.stamp();core=ledger.new_state(sid,now)
        attempt=dict(core_revision=0,core_generations=deepcopy(core['generations']),
                     core_sha256=ledger.digest(core),boot_id=now.boot_id)
        outer_reconcile.core_successor(core,attempt)
        with self.assertRaisesRegex(ValueError,'regressed'):
            outer_reconcile.core_successor(core,dict(attempt,core_revision=1))
        with self.assertRaisesRegex(ValueError,'replaced'):
            outer_reconcile.core_successor(dict(core,desired='RUNNING'),attempt)
        changed=deepcopy(core);changed['revision']=1;changed['generations']['gateway']=1
        changed['active']['gateway']=dict(generation=1,owner=dict(boot_id=str(uuid.uuid4())))
        with self.assertRaisesRegex(ValueError,'does not belong'):
            outer_reconcile.core_successor(changed,attempt)
        changed['active']['gateway']['owner']=None
        with self.assertRaisesRegex(ValueError,'does not belong'):
            outer_reconcile.core_successor(changed,dict(attempt,core_generations=dict(
                attempt['core_generations'],gateway=1)))


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--worker': worker(Path(sys.argv[2]))
    elif len(sys.argv)==4 and sys.argv[1]=='--case':
        print(json.dumps(scenario(sys.argv[2],Path(sys.argv[3]))))
    else: unittest.main(module=None)
