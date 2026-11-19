"""CPU rejection fixtures; none qualify real Prompt execution or recovery."""
import copy
import hashlib
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_recovery import profile_acceptance as driver
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_migration as migration
from tools.serve_recovery import profile_prompt as prompt
from tools.serve_recovery import profile_prompt_evidence as p
from tools.serve_recovery import test_profile_conversation as previous
from tools.serve_recovery import test_profile_migration as old
from tools.serve_service import capacity


def declaration():
    return dict(previous.declaration(), profile='q4t-native-realtime-chat-v2',
                prompt=copy.deepcopy(p.DECLARATION), pending_execution=['audio', 'mcp', 'tracing'])


def record():
    csv = 'stage,tests\nfiles,30\nruntime,22\n'
    source = dict(csv=csv, sha256=hashlib.sha256(csv.encode()).hexdigest(), sum=52,
        receipts=[dict(label=label, path='docs/evidence/serve-realtime-prompt-'+label+
            '-cpu-20261120/acceptance.json', sha256='a'*64, tests=count)
            for label, count in [('files', 30), ('runtime', 22)]])
    rows, events = [], []
    for i, answer in enumerate(('42', '377', '42', '52')):
        item = dict(id='item_'+str(i), type='message', status='completed', role='assistant',
                    content=[dict(type='output_text', text=answer)])
        row = dict(id='response_'+str(i), status='completed', output_modalities=['text'],
            metadata=dict(acceptance_case=p.CASES[i]), output=[item],
            usage=dict(input_tokens=100, output_tokens=5, total_tokens=105))
        rows.append(row)
        events.extend([dict(type='response.created', response=dict(id=row['id'])),
            dict(type='response.output_text.delta', response_id=row['id'], item_id=item['id'], delta=answer),
            dict(type='response.done', response=copy.deepcopy(row))])
    events[:0] = [dict(type='session.updated', session=dict(max_output_tokens=128,
                    prompt=dict(id='prompt_cpu', version='1'))) for _ in range(2)]
    operations = [dict(label=label, method=method, route=route, status=200, body=body)
        for label, method, route, body in [
            ('register-qualified-arithmetic', 'POST', '/v1/prompts', dict(id='prompt_cpu')),
            ('append-qualified-second', 'POST', '/v1/prompts/prompt_cpu/versions', dict(default_version='2')),
            ('delete-qualified-arithmetic', 'DELETE', '/v1/prompts/prompt_cpu', dict(deleted=True)),
            ('register-qualified-file', 'POST', '/v1/prompts', dict(id='prompt_file'))]]
    before = dict(admitted=2, settled=150, reserved=0, active=0, sessions=0, host_bytes=0, free_slots=1)
    return dict(source=source, before=before, after=dict(before, admitted=6, settled=570),
        session=dict(status='PASS', mode='qualification', transport='official-openai-wss',
            connections=1, close_code=1000, automatic_replay=False, response_creates=4, http_calls=4,
            owned=dict(prompt='prompt_cpu'), operations=operations, responses=rows, events=events))


def change_row(value, index, change):
    session = value['session']; row = session['responses'][index]; change(row)
    for e in session['events']:
        if e['type'] == 'response.done' and e['response']['id'] == row['id']:
            e['response'] = copy.deepcopy(row)


class PromptEvidence(unittest.TestCase):
    def test_new_declaration_does_not_upgrade_legacy_evidence(self):
        legacy = dict(realtime=dict(conversation=previous.declaration()))
        current = dict(realtime=dict(conversation=declaration()))
        self.assertFalse(evidence.prompt_enabled(legacy))
        self.assertTrue(evidence.prompt_enabled(current))
        self.assertEqual(evidence.format_for({}, {}, legacy), evidence.CONVERSATION_FORMAT)
        self.assertEqual(evidence.format_for({}, {}, current), evidence.PROMPT_FORMAT)
        for mutate in [lambda v: v['prompt'].update(rpm='twice'),
                       lambda v: v['prompt'].update(explicit_fields_override=1),
                       lambda v: v.update(pending_execution=['audio', 'prompt']),
                       lambda v: v.update(profile='q4t-native-realtime-chat-v1'),
                       lambda v: v.pop('prompt')]:
            changed = declaration(); mutate(changed)
            with self.assertRaises(ValueError):
                evidence.prompt_enabled(dict(realtime=dict(conversation=changed)))

    def test_csv_requires_actual_declared_source_derivation(self):
        value = record()['source']; p.source(value)
        for mutate in [lambda v: v.update(sum=53), lambda v: v.update(csv='stage,tests\nx,52\n'),
                       lambda v: v['receipts'][0].update(path='/invented'),
                       lambda v: v['receipts'][0].update(tests=True),
                       lambda v: v['receipts'][1].update(sha256='bad')]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(ValueError): p.source(changed)

    def test_four_actual_responses_and_settlement_are_immutable(self):
        value = record(); before = copy.deepcopy(value)
        self.assertEqual(p.business(value), 420)
        self.assertEqual(value, before)

    def test_failed_missing_replayed_stream_or_answer_cannot_qualify(self):
        for mutate in [lambda v: v['session'].update(status='FAIL'),
                       lambda v: v['session'].update(connections=True),
                       lambda v: v['session']['events'].append(v['session']['events'][-1]),
                       lambda v: v['session']['events'].pop(3),
                       lambda v: change_row(v, 3, lambda r: r['output'][0]['content'][0].update(text='53')),
                       lambda v: change_row(v, 0, lambda r: r['usage'].update(total_tokens=0)),
                       lambda v: change_row(v, 0, lambda r: r.update(conversation_id='leaked'))]:
            value = record(); mutate(value)
            with self.assertRaises(ValueError): p.business(value)

    def test_version_deletion_and_pinning_must_have_real_operations(self):
        for mutate in [lambda v: v['session']['events'][0]['session']['prompt'].update(version='2'),
                       lambda v: v['session']['operations'][2]['body'].update(deleted=False),
                       lambda v: v['session']['operations'][1]['body'].update(default_version='1'),
                       lambda v: v['session']['operations'][0].update(status=400),
                       lambda v: v['session']['operations'].reverse()]:
            value = record(); mutate(value)
            with self.assertRaises(ValueError): p.business(value)

    def test_preparation_cannot_consume_second_rpm_or_hide_tokens(self):
        for field, changed in [('admitted', 7), ('settled', 569), ('reserved', 1),
                               ('sessions', 1), ('host_bytes', True), ('free_slots', 0)]:
            value = record(); value['after'][field] = changed
            with self.assertRaises(ValueError): p.business(value)


class PromptDriver(unittest.TestCase):
    def test_explicit_profile_budget_preserves_original_scope(self):
        profile = capacity.normalize(dict(version=1))
        before = driver.limits_for(profile, conversation=True)
        after = driver.limits_for(profile, conversation=True, prompt=True)
        self.assertEqual(after['chats'] - before['chats'], 8)
        self.assertEqual(after['websocket_sessions'] - before['websocket_sessions'], 2)
        self.assertEqual(after['ingress_starts'] - before['ingress_starts'], 2)
        self.assertEqual(after['prompt_registry_requests'], 8)
        for args in [dict(prompt=True), dict(conversation=True, prompt=1)]:
            with self.assertRaises(RuntimeError): driver.limits_for(profile, **args)

    def test_cli_keeps_prompt_reference_explicit(self):
        with mock.patch.object(driver, 'prepare') as prepare, mock.patch('sys.argv', [
            'profile_acceptance.py', 'prepare', '--binary', '/bin', '--package', '/pkg',
            '--output', '/out', '--conversation-reference', '/chat', '--prompt-reference', '/prompt']):
            driver.main()
        self.assertEqual(prepare.call_args.args[0].prompt_reference, Path('/prompt'))

    def test_failed_sdk_closes_owned_entry_without_success_receipt(self):
        parent = prompt.ROOT / '.q4t-work/prompt-profile-cpu'; parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as tmp:
            out = Path(tmp); charged = []
            inputs = dict(format='q4t-profile-prompt-inputs-v1', files={}, sdk_directory='/sdk',
                ingress='/entry', public_port=3, tls=dict(certificate='/ca'),
                csv_path='/csv', source=record()['source'])
            stage = SimpleNamespace(out=out, profile_name='primary', history={}, budget=charged.append,
                model=SimpleNamespace(poll=lambda: None), http=lambda *a, **k: (200, b''),
                plan=dict(prompt=inputs, port=1, operations_port=2,
                          sdk=dict(sdk_directory='/sdk', node=dict(path='/node'))))
            prompt.save(out / 'client-policy.json', dict(gateway_token='cpu',
                clients=[dict(id=prompt.realtime.encrypted.CLIENT)]))
            edge = SimpleNamespace(process=SimpleNamespace(pid=99, returncode=0), close=mock.Mock())
            with (mock.patch.object(prompt, 'NativeIngress', return_value=edge),
                  mock.patch.object(prompt.realtime, 'state', return_value=record()['before']),
                  mock.patch.object(prompt.realtime.encrypted.foreground.base.lifecycle.guard,
                                    'processes', return_value={99: {}}),
                  mock.patch.object(prompt.subprocess, 'run', side_effect=subprocess.TimeoutExpired('sdk', 370))):
                with self.assertRaises(subprocess.TimeoutExpired): prompt.run(stage)
            edge.close.assert_called_once()
            self.assertEqual(charged, ['ingress_starts', 'websocket_sessions'] +
                             ['chats'] * 4 + ['prompt_registry_requests'] * 4)
            self.assertTrue((out / 'primary-prompt-entry-cleanup.json').exists())
            self.assertFalse((out / 'primary-prompt-business.json').exists())


class PromptMigration(unittest.TestCase):
    setUp = previous.ConversationMigration.setUp
    stamp = previous.ConversationMigration.stamp

    def test_automatic_fallback_cannot_drop_prompt_or_reuse_old_receipt(self):
        _, first = previous.ConversationMigration.pair(self)
        first = old.bind_records(self.old, {n: copy.deepcopy(r['record']) for n, r in first.items()})
        rows = {n: copy.deepcopy(r['record']) for n, r in first.items()}
        for row in rows.values():
            row['format'] = evidence.PROMPT_FORMAT
            row['capabilities']['realtime']['conversation'] = declaration()
            row['business']['prompt'] = record()
        second = old.bind_records(self.new, rows)
        original = copy.deepcopy((first, second))
        self.assertFalse(migration.permit(self.old, self.new, first, second,
            self.config, self.config, allow_capability_extension=True)['automatic'])
        with self.assertRaisesRegex(ValueError, 'capabilities'):
            migration.permit(self.new, self.old, second, first, self.config, self.config, automatic=True)
        same_registry = copy.deepcopy(self.old)
        same = old.bind_records(same_registry, copy.deepcopy(rows))
        self.assertTrue(migration.permit(self.new, same_registry, second, same,
            self.config, self.config, automatic=True)['automatic'])
        self.assertEqual((first, second), original)
        for change in [lambda r: r['business'].pop('prompt'),
                       lambda r: r.update(format=evidence.CONVERSATION_FORMAT)]:
            wrong = copy.deepcopy(rows); change(wrong['primary'])
            registry = copy.deepcopy(self.new); bound = old.bind_records(registry, wrong)
            with self.assertRaises(ValueError):
                evidence.validate(bound['primary']['record'], registry, 'primary', bound['primary']['sha256'])

    def test_normalization_preserves_prompt_and_rejects_unknown_contracts(self):
        legacy = dict(realtime=dict(conversation=previous.declaration()))
        current = dict(realtime=dict(conversation=declaration()))
        before = copy.deepcopy((legacy, current))
        first, second = map(migration.semantic_capabilities, (legacy, current))
        self.assertTrue(migration.extends(first, second))
        self.assertNotEqual(first, second)
        self.assertEqual(second['realtime']['conversation']['prompt'], p.DECLARATION)
        self.assertEqual((legacy, current), before)
        for change in [lambda c: c.update(profile='q4t-native-realtime-chat-v3'),
                       lambda c: c.update(max_output_tokens=1),
                       lambda c: c['prompt'].update(maximum_expansion_bytes=0),
                       lambda c: c.update(pending_execution=[])]:
            wrong = copy.deepcopy(current); change(wrong['realtime']['conversation'])
            with self.assertRaises(ValueError): migration.semantic_capabilities(wrong)


if __name__ == '__main__': unittest.main()
