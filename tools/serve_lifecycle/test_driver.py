"""Negative acceptance controls: terminals, resource growth and lost identity."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from tools.serve_lifecycle import stage, verify, observe
from tools.serve_ops import fixtures

h = stage.h


def wire(*, finish=None):
    chunk = dict(id='lifecycle-test', model='qwen3.8-flash-next', created=1,
        object='chat.completion.chunk', choices=[dict(index=0,
            delta=dict(role='assistant', content='1'), finish_reason=finish, logprobs=None)])
    return ('data: '+json.dumps(chunk)+'\n\n').encode()


def shutdown():
    value = dict(error=dict(code='server_shutdown', message='shutdown', type='server_error', param=None))
    return wire()+('data: '+json.dumps(value)+'\n\ndata: [DONE]\n\n').encode()


class Driver(unittest.TestCase):
    def setUp(self):
        stage.CPU.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=stage.CPU)
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.validator = Draft202012Validator(h.read(stage.ROOT/'docs/serve-transport-v1.schema.json'))

    def test_complete_shutdown_and_unknown_crash_are_distinct(self):
        ended = verify.terminal(shutdown(), graceful=True, validator=self.validator)
        self.assertTrue(ended['terminal_complete']);self.assertFalse(ended['success'])
        broken = verify.terminal(wire()+b'data: {"unfinished', graceful=False, validator=self.validator)
        self.assertFalse(broken['terminal_complete'])
        self.assertEqual(broken['outcome'], 'REQUEST_OUTCOME_UNKNOWN')

    def test_missing_done_duplicate_error_wrong_reason_and_false_success_are_rejected(self):
        for raw in (shutdown().replace(b'data: [DONE]\n\n', b''), shutdown()+b'data: [DONE]\n\n',
                    shutdown()+shutdown(), shutdown().replace(b'server_shutdown', b'explicit_cancel'),
                    wire(finish='stop')+shutdown(), wire()):
            with self.subTest(raw=raw), self.assertRaises((RuntimeError, ValueError)):
                verify.terminal(raw, graceful=True, validator=self.validator)
        for raw in (shutdown(), wire(finish='length'), wire()+b'data: [DONE]\n\n', b''):
            with self.assertRaises((RuntimeError, ValueError)):
                verify.terminal(raw, graceful=False, validator=self.validator)

    def test_frozen_attempts_and_observation_budget_fail_before_increment(self):
        run = verify.Run.__new__(verify.Run)
        run.deadline, run.monitor, run.control_count = 100, None, stage.LIMITS['observations']
        run.counts = {k:stage.LIMITS[k] for k in stage.PLANNED}
        with patch.object(verify.time, 'time', return_value=99):
            for kind in stage.PLANNED:
                with self.assertRaises(RuntimeError):run.budget(kind)
            with self.assertRaises(RuntimeError):run.control()
        self.assertEqual(run.control_count, stage.LIMITS['observations'])
        with patch.object(verify.time, 'time', return_value=100), self.assertRaises(RuntimeError):
            run.budget()

    def test_preflight_budget_rejects_without_http(self):
        run = verify.Run.__new__(verify.Run);run.preflights = stage.LIMITS['preflights']
        with patch.object(run, 'http', side_effect=AssertionError('network')), self.assertRaises(RuntimeError):
            run.preflight()
        self.assertEqual(run.preflights, stage.LIMITS['preflights'])

    def test_counter_loss_failed_requests_and_worker_leaks_are_rejected(self):
        rows = fixtures.metrics(version=4)
        rows.update(q4t_requests_total=5, q4t_requests_success_total=5,
                    q4t_structured_workers_started_total=3, q4t_structured_workers_reaped_total=3)
        verify.accounting(rows, 5, 3)
        for key in ('q4t_requests_total', 'q4t_requests_success_total', 'q4t_requests_error_total',
                    'q4t_requests_aborted_total', 'q4t_structured_workers_started_total',
                    'q4t_structured_workers_reaped_total'):
            with self.assertRaises(RuntimeError):verify.accounting(dict(rows, **{key:rows[key]+1}), 5, 3)

    def test_one_hour_envelope_requires_all_cycles_and_every_resource(self):
        row = dict(parent=dict(VmRSS=1024, fds=20, Threads=5), resources=dict(cgroup=dict(
            observations={'memory.stat':dict(value=dict(anon=2048))})))
        rows = [copy.deepcopy(row) for _ in range(stage.CYCLES)]
        result = observe.plateau(rows)
        self.assertEqual((result['warmup_cycles'], result['measured_cycles']), (16, 224))
        with self.assertRaises(RuntimeError):observe.plateau(rows[:-1])
        for key, delta in (('VmRSS', 64*1024**2+1), ('fds', 3), ('Threads', 3)):
            changed = copy.deepcopy(rows);changed[-1]['parent'][key] += delta
            with self.assertRaises(RuntimeError):observe.plateau(changed)
        rows[-1]['resources']['cgroup']['observations']['memory.stat']['value']['anon'] += 128*1024**2+1
        with self.assertRaises(RuntimeError):observe.plateau(rows)

    def test_observation_failure_is_environment_abort_not_pass_or_zero(self):
        for error in (RuntimeError('missing counter'), OSError('cannot read resource')):
            with patch.object(observe.prior, 'sample', side_effect=error), self.assertRaises(observe.ObservationAbort):
                observe.sample(None, 1)
        with patch.object(observe.prior, 'sample', return_value=dict(process_memory=dict(sampling_errors=['bad']))), \
             self.assertRaises(observe.ObservationAbort):observe.sample(None, 1)

    def test_exited_zombie_recycled_and_live_workers_are_distinguished(self):
        def proc(pid, start, state):
            path = self.folder/str(pid);path.mkdir()
            fields = [state, '2']+['0']*17+[str(start)]
            (path/'stat').write_text(str(pid)+' (worker) '+' '.join(fields))
        proc(10, 11, 'Z');proc(20, 21, 'S');proc(30, 31, 'S')
        result = observe.child_states({'10':11, '20':21, '30':30, '40':41}, proc=self.folder)
        self.assertEqual([r['state'] for r in result], ['zombie', 'present', 'pid_reused', 'absent'])
        self.assertEqual([r['running'] for r in result], [False, True, False, False])

    def test_worker_still_live_fails_before_any_controller_cleanup(self):
        with patch.object(observe, 'child_states', return_value=[dict(running=True)]), \
             self.assertRaises(RuntimeError):observe.wait_children({'10':11}, self.folder, timeout=0)
        saved = h.read(self.folder/'child-exit-observations.json')
        self.assertTrue(saved['observed_before_scope_cleanup'])
        self.assertFalse(saved['controller_signalled_workers'])

    def test_crash_journal_uses_actual_prefix_and_inspector_does_not_mutate(self):
        # Exercise the actual journal writer with a local transport substitute;
        # there is no HTTP listener, model, fabricated success or replay here.
        caps = json.loads(h.read(stage.PRIOR/'execution/business/model/capabilities.json')['raw'])
        body = verify.base.ordinary(stream=True)
        client = verify.JournalClient(1, self.folder, 'crash', body, caps)
        def interrupted(_self, body, *, sink):
            sink(wire());raise verify.ProtocolError('EOF without DONE')
        captured = bytearray()
        with patch.object(verify.FullWireClient, 'complete', interrupted), self.assertRaises(verify.ProtocolError):
            client.complete(body, sink=captured.extend)
        before = {p.name:p.read_bytes() for p in client.journal.iterdir()}
        result = verify.tool_recovery.inspect_recovery(client.journal)
        self.assertEqual(result['state'], 'REQUEST_OUTCOME_UNKNOWN')
        self.assertEqual(result['requests'], 1)
        self.assertFalse(result['automatic_retry'])
        self.assertEqual(captured, before['round-00-wire.bin'])
        self.assertEqual(before, {p.name:p.read_bytes() for p in client.journal.iterdir()})


if __name__ == '__main__':unittest.main()
