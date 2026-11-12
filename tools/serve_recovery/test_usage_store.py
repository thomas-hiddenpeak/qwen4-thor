"""CPU durable usage transitions, recovery faults and actual native snapshots."""
import copy
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_policy, access_store, journal, ledger
from tools.serve_recovery import quota_policy, quota_store, usage_policy as p, usage_store as s
from tools.serve_recovery.test_gateway import config
from tools.serve_recovery.test_quota_admin import ROOT, Server
from tools.serve_recovery.test_quota_usage_admin import request


def owner(boot=None, pid=100):
    return dict(pid=pid, start_ticks=123, boot_id=boot or str(uuid.uuid4()))


def snapshot(now=200, clients=2):
    return dict(version=1, captured_ns=str(now), policy_revision='0', policy_sha256='a'*64,
        clients=[dict(id='client_'+str(i), window_started=True, window_ns=str(now-10),
            requests='1', tokens='5', rpm_rejected='0', tpm_rejected='0', admitted='3',
            settled_tokens='15') for i in range(clients)])


class UsageStoreTests(unittest.TestCase):
    def test_batch_snapshot_durable_handoff_and_cross_boot_windows(self):
        value = snapshot()
        value['version'] = 2
        for client in value['clients']:
            client['batch'] = {k: v for k, v in client.items() if k != 'id'}
            client['batch'].update(requests='2', tokens='8',
                                    admitted='5', settled_tokens='21')
        self.execute('begin')
        self.execute('seal', value=value, now=200)
        active = self.execute('begin', generation=2, now=300)
        self.assertEqual(p.restoration(active, self.owner['boot_id'], 350)
                         ['snapshot'], value)
        # A new boot conserves both totals, resetting only minute windows.
        later = copy.deepcopy(value)
        later['captured_ns'] = '400'
        self.execute('seal', generation=2, value=later, now=400)
        other = owner()
        rebooted = self.execute('begin', generation=3, model=other, now=500)
        restored = p.restoration(rebooted, other['boot_id'], 600)['snapshot']
        self.assertEqual(restored['clients'][0]['settled_tokens'], '15')
        self.assertEqual(restored['clients'][0]['batch']['settled_tokens'], '21')
        for client in restored['clients']:
            for window in [client, client['batch']]:
                self.assertFalse(window['window_started'])
                self.assertEqual(window['tokens'], '0')
                self.assertEqual(window['requests'], '0')

    def test_batch_snapshot_maximum_both_pools_and_invalid_shape(self):
        value = snapshot(clients=64)
        value['version'] = 2
        for client in value['clients']:
            for key in p.COUNTS:
                client[key] = str(2**64-1)
            client['batch'] = {k: v for k, v in client.items() if k != 'id'}
        p.snapshot(value)
        self.assertLessEqual(len(ledger.canonical(value)), p.MAX_SNAPSHOT_BYTES)
        for mutate in [
            lambda v: v['clients'][0].pop('batch'),
            lambda v: v['clients'][0]['batch'].update(window_started=1),
            lambda v: v['clients'][0]['batch'].update(settled_tokens='0'),
            lambda v: v.update(version=1),
        ]:
            bad = copy.deepcopy(value)
            mutate(bad)
            with self.assertRaises(ValueError):
                p.snapshot(bad)

    def test_batch_snapshot_unknown_commit_keeps_both_pools_exact(self):
        self.execute('begin')
        value = snapshot()
        value['version'] = 2
        for client in value['clients']:
            client['batch'] = {k: v for k, v in client.items() if k != 'id'}
        with s.open_bound(self.binding) as writer:
            previous = writer.head['state']['policy']
            candidate = p.seal(previous, 1, self.owner, value)
            fsync = os.fsync
            def lose(fd):
                if fd == writer.dir_fd:
                    raise OSError('Batch usage committed, receipt unknown')
                return fsync(fd)
            with mock.patch.object(access_store.os, 'fsync', side_effect=lose):
                with self.assertRaises(OSError):
                    writer.transact(str(uuid.uuid4()), 1, candidate)
        self.assertEqual(self.execute('seal', value=value, now=300), candidate)
        active = self.execute('begin', generation=2, now=400)
        self.assertEqual(active['snapshot'], value)

    def setUp(self):
        base = ROOT/'.q4t-work/serve-access-20261109/usage-store-fixtures'
        base.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=base);self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.binding = access_store.binding(self.state, str(uuid.uuid4()))
        self.owner = owner()
        s.initialize_bound(self.binding)

    def execute(self, op, *, generation=1, model=None, value=None, now=100):
        args = dict(sequence=1, op=op)
        if op != 'load':args.update(generation=generation, owner=self.owner if model is None else model)
        if op == 'seal':args['snapshot'] = snapshot() if value is None else value
        return s.execute(self.binding, args, now_ns=now)

    def test_sealed_handoff_consumption_is_durable_and_replays_do_not_reopen(self):
        active = self.execute('begin')
        self.assertEqual(self.execute('begin', now=150), active)
        self.assertEqual(p.restoration(active, self.owner['boot_id'], 150)['mode'], 'unknown')
        sealed = self.execute('seal', now=200)
        self.assertEqual(self.execute('seal', now=250), sealed)
        with self.assertRaises(ValueError):self.execute('begin', now=300)
        next_owner = dict(self.owner, pid=101)
        active = self.execute('begin', generation=2, model=next_owner, now=300)
        self.assertEqual(p.restoration(active, next_owner['boot_id'], 400),
                         dict(mode='exact', snapshot=snapshot()))
        self.assertEqual(self.execute('load'), active)
        with self.assertRaises(ValueError):self.execute('seal', now=500)

    def test_crash_or_skipped_generation_cannot_reuse_an_old_sealed_snapshot(self):
        self.execute('begin');self.execute('seal', now=200)
        second = self.execute('begin', generation=2, now=300)
        self.assertEqual(p.restoration(second, self.owner['boot_id'], 300)['mode'], 'exact')
        crashed = self.execute('begin', generation=3, now=400)
        self.assertIsNone(crashed['snapshot'])
        self.assertEqual(p.restoration(crashed, self.owner['boot_id'], 400)['mode'], 'unknown')
        self.execute('seal', generation=3, value=snapshot(500), now=500)
        skipped = self.execute('begin', generation=5, now=600)
        self.assertIsNone(skipped['source']);self.assertIsNone(skipped['snapshot'])
        with self.assertRaises(ValueError):self.execute('begin', generation=4, now=700)

    def test_cross_boot_keeps_totals_but_never_reuses_monotonic_window(self):
        self.execute('begin');self.execute('seal', now=200)
        new_owner = owner()
        active = self.execute('begin', generation=2, model=new_owner, now=50)
        restored = p.restoration(active, new_owner['boot_id'], 60)
        self.assertEqual(restored, p.restoration(active, new_owner['boot_id'], 70))
        self.assertEqual(restored['mode'], 'cross_boot')
        client = restored['snapshot']['clients'][0]
        self.assertEqual((client['requests'], client['tokens'], client['admitted'],
                          client['settled_tokens']), ('0', '0', '3', '15'))
        self.assertFalse(client['window_started']);self.assertEqual(client['window_ns'], '0')
        with self.assertRaises(ValueError):p.restoration(active, self.owner['boot_id'], 60)
        with self.assertRaises(ValueError):p.restoration(active, new_owner['boot_id'], 49)
        with self.assertRaises(ValueError):self.execute('seal', generation=2, model=new_owner,
                                                      value=snapshot(40), now=60)

    def test_interrupted_activation_consumes_snapshot_before_any_later_restore(self):
        self.execute('begin');self.execute('seal', now=200)
        with mock.patch.object(access_store.os, 'replace', side_effect=OSError('CPU pre-rename')):
            with self.assertRaises(OSError):self.execute('begin', generation=2, now=300)
        with mock.patch.object(access_store.os, 'fsync', side_effect=OSError('CPU unknown durability')):
            with self.assertRaises(OSError):self.execute('load')
        active = self.execute('begin', generation=2, now=400)
        self.assertEqual(active['started_ns'], '300')
        self.assertEqual(active['revision'], 3)
        self.assertEqual(p.restoration(active, self.owner['boot_id'], 400)['mode'], 'exact')
        self.assertIsNone(self.execute('begin', generation=3, now=500)['source'])

    def test_unknown_seal_sync_recovers_exactly_and_foreign_identity_is_refused(self):
        self.execute('begin')
        with s.open_bound(self.binding) as writer:
            previous = writer.head['state']['policy']
            candidate = p.seal(previous, 1, self.owner, snapshot())
            original = os.fsync
            def fail(fd):
                if fd == writer.dir_fd:raise OSError('CPU post-rename')
                return original(fd)
            with mock.patch.object(access_store.os, 'fsync', side_effect=fail):
                with self.assertRaises(OSError):writer.transact(str(uuid.uuid4()), 1, candidate)
        self.assertEqual(self.execute('seal', now=300), candidate)
        # Python equality must not accept bool/int aliases on a replay path.
        bad=snapshot();bad['version']=True
        with self.assertRaises(ValueError):self.execute('seal',value=bad,now=300)
        bad=snapshot();bad['clients'][0]['window_started']=1
        with self.assertRaises(ValueError):self.execute('seal',value=bad,now=300)
        with self.assertRaises(ValueError):self.execute('seal',now=True)
        with self.assertRaises(ValueError):self.execute('seal', model=dict(self.owner, start_ticks=124), now=300)
        changed = snapshot();changed['clients'][0]['admitted'] = '4'
        with self.assertRaises(ValueError):self.execute('seal', value=changed, now=300)
        active = self.execute('begin', generation=2, now=400)
        self.assertEqual(active['snapshot'], snapshot())

    def test_missing_usage_never_reinitializes_and_cannot_mutate_keys_or_limits(self):
        access_store.initialize_bound(self.binding, access_policy.from_keys([
            dict(id='alpha', api_keys=['a'*40])]))
        quota_store.initialize_bound(self.binding, quota_policy.from_base(config(self.state)['backend_policy']))
        paths=[self.state/'client-access/policy.json',self.state/'client-limits/policy.json']
        before=[f.read_bytes() for f in paths]
        self.execute('begin');self.execute('seal', now=200)
        path=self.state/'client-usage/policy.json';original=path.read_bytes();path.write_bytes(b'{')
        with self.assertRaises(ValueError):self.execute('load')
        with self.assertRaises(ValueError):s.initialize_bound(self.binding)
        path.write_bytes(original)
        shutil.rmtree(self.state/'client-usage')
        with self.assertRaises(OSError):s.initialize_bound(self.binding)
        self.assertFalse((self.state/'client-usage').exists())
        self.assertEqual([f.read_bytes() for f in paths],before)

    def test_max_clients_receipt_retention_and_wire_values_are_bounded(self):
        huge = snapshot(clients=64)
        for client in huge['clients']:
            for key in p.COUNTS:client[key] = str(2**64-1)
        p.snapshot(huge)
        for generation in range(1, 67):
            self.execute('begin', generation=generation, now=generation*100)
            huge['captured_ns']=str(generation*100+50)
            for client in huge['clients']:client['window_ns']=str(generation*100+40)
            self.execute('seal', generation=generation, value=huge, now=generation*100+50)
        with s.open_bound(self.binding) as writer:
            self.assertEqual(len(writer.head['state']['receipts']),128)
        self.assertLess((self.state/'client-usage/policy.json').stat().st_size,p.MAX_BYTES)
        for bad in (True, 1.0, 1, '-1', '00', str(2**64)):
            value=copy.deepcopy(huge);value['clients'][0]['requests']=bad
            with self.assertRaises(ValueError):p.snapshot(value)
        value=copy.deepcopy(huge);value['clients'][1]['id']=value['clients'][0]['id']
        with self.assertRaises(ValueError):p.snapshot(value)
        self.assertEqual(len(list((self.state/'client-usage').iterdir())),2)

    def test_actual_native_snapshot_journal_and_successor_keep_exhausted_quota(self):
        policy=quota_policy.runtime(quota_policy.from_base(config(self.state)['backend_policy']),'a'*64)
        for client in policy['clients']:client['requests_per_minute']=1
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        old=Server(policy);self.addCleanup(old.close)
        first=dict(pid=old.process.pid,start_ticks=journal.process_start(old.process.pid),boot_id=boot)
        self.execute('begin',model=first,now=time.monotonic_ns())
        self.assertTrue(request(old,'usage_restore')['ok'])
        self.assertTrue(old.command('reserve',id='alpha')['ok']);old.command('settle')
        closed=request(old,'usage_freeze')['usage']['snapshot'];p.snapshot(closed)
        self.execute('seal',model=first,value=closed,now=time.monotonic_ns())
        successor=Server(policy);self.addCleanup(successor.close)
        second=dict(pid=successor.process.pid,start_ticks=journal.process_start(successor.process.pid),boot_id=boot)
        active=self.execute('begin',generation=2,model=second,now=time.monotonic_ns())
        restore=p.restoration(active,boot,time.monotonic_ns())
        self.assertEqual(restore['mode'],'exact')
        self.assertTrue(request(successor,'usage_restore',restore['snapshot'])['ok'])
        self.assertFalse(successor.command('reserve',id='alpha')['ok'])
        self.assertEqual(successor.command('status')['tokens'],5)


if __name__ == '__main__':unittest.main()
