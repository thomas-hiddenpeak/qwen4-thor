"""Meaningful CPU boundary and privacy checks; no HTTP or model is launched."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from tools.serve_ops import bundle, contracts, diagnose, fixtures, identity
from tools.serve_ops import privacy, retention, transport
from tools.serve_ops.contracts import InvalidObservation

WORK = fixtures.ROOT/'.q4t-work/serve-ops-20261103/cpu/tmp'


def instance():
    return dict(pid=12345, start_ticks=12000, boot_id='1'*36,
        executable_sha256='a'*64, net_namespace_device=1, net_namespace_inode=2,
        listener_inode=501, listener_scope='loopback')


def report():
    return dict(version=contracts.VERSION, unix_seconds=time.time(),
        monotonic=time.monotonic(), port=18107,
        identity=dict(state='verified', process=instance()),
        configuration=dict(protocol_sha256='b'*64),
        metrics=contracts.parse_metrics(fixtures.text_metrics())[0])


class Files(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=WORK)
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)


class MetricContract(unittest.TestCase):
    def test_real_archived_family_projection_and_integer_precision(self):
        values = fixtures.metrics()
        values['q4t_generation_tokens_total'] = 2**64-1
        selected, ignored = contracts.parse_metrics(fixtures.text_metrics(values))
        self.assertEqual(len(selected), 87)
        self.assertEqual(selected['q4t_generation_tokens_total'], 2**64-1)
        self.assertGreater(ignored, 0)  # Histograms are explicitly outside projection.

    def test_no_silent_missing_duplicate_or_label_replacement(self):
        raw = fixtures.text_metrics()
        for broken in (raw.replace('q4t_ready 1\n',''), raw+'q4t_ready 1\n',
                       raw.replace('q4t_ready 1', 'q4t_ready{secret="private"} 1'),
                       raw.replace('state="open"', 'state="open",state="open"'),
                       raw[:-1]):
            with self.subTest(broken=broken[-80:]), self.assertRaises(InvalidObservation):
                contracts.parse_metrics(broken)

    def test_nonfinite_fractional_negative_and_overflow_rejected(self):
        for value in ('NaN', '+Inf', '-1', '1.1', str(2**64), '1e10000000', '2'):
            with self.subTest(value=value), self.assertRaises(InvalidObservation):
                contracts.parse_metrics(fixtures.text_metrics().replace('q4t_ready 1', 'q4t_ready '+value))

    def test_disabled_and_unknown_are_distinct(self):
        values = fixtures.metrics()
        values['q4t_memory_admission_state{state="open"}'] = 0
        values['q4t_memory_observation_valid'] = 0
        values['q4t_memory_available_bytes'] = 0
        values['q4t_memory_admission_state{state="memory_observation_unavailable"}'] = 1
        values['q4t_memory_admission_open'] = 0
        contracts.parse_metrics(fixtures.text_metrics(values))
        values['q4t_memory_admission_enabled'] = 0
        values['q4t_memory_admission_open'] = 1
        values['q4t_memory_admission_state{state="memory_observation_unavailable"}'] = 0
        values['q4t_memory_admission_state{state="disabled"}'] = 1
        contracts.parse_metrics(fixtures.text_metrics(values))
        values['q4t_memory_admission_open'] = 0
        with self.assertRaises(InvalidObservation):
            contracts.parse_metrics(fixtures.text_metrics(values))

    def test_impossible_states_slots_and_untrusted_expansion(self):
        raw = fixtures.text_metrics()
        for broken in (raw.replace('state="disabled"} 0', 'state="disabled"} 1'),
                       raw.replace('q4t_seq_slots_free 1', 'q4t_seq_slots_free 2')):
            with self.assertRaises(InvalidObservation):
                contracts.parse_metrics(broken)
        result, _ = contracts.parse_metrics(raw+'secret_metric{body="PRIVATE"} 10\n')
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_json_duplicate_depth_nonfinite_and_root(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":1e1000}', '[]', '{"x":'+ '['*2000+'0'+']'*2000+'}'):
            with self.assertRaises(InvalidObservation):
                contracts.json_object(raw)
        self.assertEqual(contracts.json_object('{"n":9007199254740993}')['n'], 9007199254740993)
        self.assertEqual(contracts.json_object(json.dumps({'s':'["\\'*100}))['s'], '["\\'*100)

    def test_configuration_projection_and_types(self):
        value = fixtures.configuration()
        value['model'] = value['argv'] = 'PRIVATE'
        selected = contracts.configuration(value)
        self.assertNotIn('PRIVATE', json.dumps(selected))
        for field in ('runtime', 'memory_admission', 'max_sequences', 'protocol_source_sha256'):
            bad = copy.deepcopy(value)
            bad[field] = []
            with self.subTest(field=field), self.assertRaises(InvalidObservation):
                contracts.configuration(bad)

    def test_readiness_status_contract(self):
        contracts.readiness(dict(status='memory_pressure', live=True, ready=False), 503)
        contracts.readiness(dict(status='unhealthy', live=True, ready=False), 200, liveness=True)
        for value, status in [(dict(status='ready', live=True, ready=False),200),
                              (dict(status='ready', live=1, ready=True),200),
                              (dict(status='ready', live=True, ready=True),503)]:
            with self.assertRaises(InvalidObservation):
                contracts.readiness(value,status)


class Transport(unittest.TestCase):
    def test_only_direct_numeric_loopback(self):
        self.assertEqual(transport.endpoint('http://127.0.0.1:18107/'),18107)
        for url in ('https://127.0.0.1:80','http://localhost:80','http://127.0.0.1',
                    'http://secret@127.0.0.1:80','http://127.0.0.1:80?a=b',
                    'http://127.0.0.1:80/private','http://[::1]:80','http://127.0.0.1:99999'):
            with self.subTest(url=url), self.assertRaises(InvalidObservation):
                transport.endpoint(url)

    def test_exact_framing_utf8_and_header_whitelist(self):
        raw = b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\nAuthorization: PRIVATE\r\n\r\n{}'
        self.assertNotIn('PRIVATE', json.dumps(transport.parse_response(raw)))
        for broken in (raw[:-1],raw+b'x',raw.replace(b'{}',b'\xff\xff'),
                       raw.replace(b'Content-Length: 2',b'Content-Length: 2\r\nContent-Length: 2'),
                       raw.replace(b'Content-Length: 2',b'Transfer-Encoding: chunked'),
                       raw.replace(b'Content-Length: 2', b'x: '+b'a'*17000)):
            with self.assertRaises(InvalidObservation):
                transport.parse_response(broken)

    def test_slow_drip_obeys_absolute_deadline_no_proxy_or_retry(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.send.return_value = 10000
        sock.recv.return_value = b'H'
        times = iter([0,0,.1,.2,.3,1.5,2.1])
        with patch.object(transport.time,'monotonic', side_effect=lambda:next(times)), \
             patch.object(transport.socket,'create_connection', return_value=sock) as connect, \
             patch.object(transport.select,'select', side_effect=lambda r,w,x,t:(r,w,x)):
            with self.assertRaisesRegex(InvalidObservation, 'http_deadline'):
                transport.get(18107,'/metrics',timeout=2)
        connect.assert_called_once_with(('127.0.0.1',18107),timeout=2)
        self.assertEqual(sock.send.call_args.args[0].split()[0],b'GET')

    def test_response_limit_and_expired_group_deadline(self):
        with patch.object(transport.time,'monotonic',return_value=5), \
             patch.object(transport.socket,'create_connection') as connect:
            with self.assertRaises(InvalidObservation):
                transport.get(18107,'/metrics',deadline=4)
            connect.assert_not_called()
        sock=MagicMock(); sock.__enter__.return_value=sock
        sock.send.return_value=10000; sock.recv.return_value=b'x'*17000
        with patch.object(transport.socket,'create_connection',return_value=sock), \
             patch.object(transport.select,'select',side_effect=lambda r,w,x,t:(r,w,x)):
            with self.assertRaisesRegex(InvalidObservation,'http_response_limit'):
                transport.get(18107,'/metrics')


class Identity(Files):
    def fake_proc(self):
        root=self.root/'proc'; directory=root/'12345'
        for path in (directory/'fd',directory/'ns',root/'self/ns',root/'sys/kernel/random',root/'net'):
            path.mkdir(parents=True,exist_ok=True)
        (directory/'stat').write_text('12345 (model) S '+' '.join(['0']*18+['12000']))
        (directory/'exe').write_bytes(b'ELF fake test fixture')
        (directory/'ns/net').touch()
        os.link(directory/'ns/net',root/'self/ns/net')
        (root/'sys/kernel/random/boot_id').write_text('12345678-1234-1234-1234-123456789012')
        (directory/'fd/3').symlink_to('socket:[501]')
        (root/'net/tcp').write_text('header\n0: 0100007F:46BB 00000000:0000 0A 0 0 0 0 0 501\n')
        return root

    def test_process_listener_executable_and_namespace_binding(self):
        root=self.fake_proc()
        value=identity.process(12345,18107,proc=root)
        self.assertEqual(value['start_ticks'],12000)
        self.assertEqual(value['listener_inode'],501)
        self.assertEqual(value['executable_sha256'],hashlib.sha256(b'ELF fake test fixture').hexdigest())
        (root/'self/ns/net').unlink(); (root/'self/ns/net').touch()
        with self.assertRaisesRegex(InvalidObservation,'network_namespace_mismatch'):
            identity.process(12345,18107,proc=root)

    def test_foreign_or_ambiguous_listener_is_unknown(self):
        text='header\n0: 0100007F:46BB 00000000:0000 0A 0 0 0 0 0 501\n'
        for raw, owned in ((text,{'999'}),(text+text.splitlines(True)[1],{'501'})):
            with self.assertRaises(InvalidObservation):
                identity.listener(raw,18107,owned)

    def test_pid_changed_mid_observation_rejected(self):
        root=self.fake_proc(); original=identity.bounded_file
        calls=0
        def changed(path,*args,**kwargs):
            nonlocal calls
            raw=original(path,*args,**kwargs)
            if Path(path).name=='stat':
                calls+=1
                if calls==2: raw=raw.replace(b'12000',b'12001')
            return raw
        with patch.object(identity,'bounded_file',side_effect=changed):
            with self.assertRaisesRegex(InvalidObservation,'process_identity_changed'):
                identity.process(12345,18107,proc=root)

    def test_release_and_file_limit_symlink_fail_closed(self):
        path=self.root/'file'; path.write_bytes(b'x'*100)
        with self.assertRaises(InvalidObservation): identity.bounded_file(path,99)
        link=self.root/'link'; link.symlink_to(path)
        with self.assertRaises(InvalidObservation): identity.release_identity(link)
        path.write_text('{"binary_sha256":"PRIVATE","chat_protocol":{}}')
        with self.assertRaises(InvalidObservation) as error: identity.release_identity(path)
        self.assertNotIn('PRIVATE',str(error.exception))


class Diagnosis(unittest.TestCase):
    def snapshot(self, fetch=fixtures.reply, processes=None):
        protocol=fixtures.configuration()['protocol_source_sha256']
        with patch.object(identity,'release_identity',return_value=dict(binary_sha256='a'*64,protocol_sha256=protocol)), \
             patch.object(identity,'process',side_effect=processes or [instance(),instance()]), \
             patch.object(transport,'get',side_effect=fetch):
            return diagnose.snapshot(18107,12345,Path('unused'))

    def test_valid_snapshot_and_precise_same_instance_delta(self):
        self.assertEqual(self.snapshot()['state'],'HEALTHY')
        before=report(); after=copy.deepcopy(before)
        after['monotonic']+=2
        before['metrics']['q4t_generation_tokens_total']=2**63
        after['metrics']['q4t_generation_tokens_total']=2**63+1
        delta=diagnose.compare(before,after)
        self.assertEqual(delta['state'],'available')
        self.assertEqual(delta['counters']['q4t_generation_tokens_total'],1)

    def test_restart_bad_previous_reset_and_stale_never_zero(self):
        current=report(); old=copy.deepcopy(current); old['monotonic']-=2
        variants=[{},dict(old,monotonic=old['monotonic']-3600)]
        for field in ('pid','start_ticks','boot_id','listener_inode'):
            bad=copy.deepcopy(old); bad['identity']['process'][field]='changed'; variants.append(bad)
        bad=copy.deepcopy(old); bad['metrics']['q4t_requests_total']=100; variants.append(bad)
        for previous in variants:
            result=diagnose.compare(previous,current)
            self.assertEqual(result['state'],'unavailable')
            self.assertEqual(result['counters'],{})

    def test_malformed_capabilities_and_protocol_never_leak(self):
        for mutation in ({'observability':[]},{'protocol_source_sha256':'PRIVATE'}):
            def fetch(port,path,**kw):
                value=fixtures.reply(port,path,**kw)
                if path=='/v1/capabilities':
                    doc=json.loads(value['body']);doc.update(mutation);value['body']=json.dumps(doc)
                return value
            value=self.snapshot(fetch)
            self.assertEqual(value['state'],'UNKNOWN')
            self.assertNotIn('PRIVATE',json.dumps(value))

    def test_observation_loss_or_changed_process_is_unknown(self):
        def fetch(port,path,**kw):
            if path=='/metrics': raise InvalidObservation('connection_refused')
            return fixtures.reply(port,path,**kw)
        value=self.snapshot(fetch)
        self.assertEqual(value['state'],'UNKNOWN'); self.assertIsNone(value['metrics'])
        changed=instance();changed['start_ticks']+=1
        self.assertEqual(self.snapshot(processes=[instance(),changed])['state'],'UNKNOWN')

    def test_gpu_failure_is_critical_and_bad_release_is_unknown(self):
        def fetch(port,path,**kw):
            result=fixtures.reply(port,path,**kw)
            if path=='/metrics': result['body']=result['body'].replace('q4t_gpu_healthy 1','q4t_gpu_healthy 0')
            return result
        self.assertEqual(self.snapshot(fetch)['state'],'CRITICAL')
        changed=instance(); changed['executable_sha256']='f'*64
        self.assertEqual(self.snapshot(processes=[changed,changed])['state'],'UNKNOWN')


class PrivacyAndRetention(Files):
    def test_allowlist_id_correlation_without_sensitive_payload(self):
        raw=('loading /secret/model PRIVATE_PROMPT\n'
             '[q4t][request] id=PRIVATE_ID trace_id=PRIVATE_ID event=end outcome=success elapsed_ms=1.2 body=PRIVATE_BODY cancel_token=PRIVATE_TOKEN\n'
             '[q4t][http] trace_id=PRIVATE_ID completion_id=PRIVATE_ID status=200\n'
             '[q4t][request] id=PRIVATE_ID reason=PRIVATE_ERROR\n'
             '[q4t][request] id=first id=PRIVATE_DUPLICATE event=end\n')
        first=privacy.project(raw,salt=b'1'*32);second=privacy.project(raw,salt=b'2'*32)
        self.assertNotIn('PRIVATE',json.dumps(first))
        self.assertEqual(first['events'][0]['id_hash'], first['events'][1]['completion_id_hash'])
        self.assertNotEqual(first['events'][0]['id_hash'],second['events'][0]['id_hash'])
        self.assertEqual(first['events'][2]['reason'],'unknown')
        self.assertEqual(first['omitted_lines'],2)

    def test_tail_truncation_event_limit_and_link_refusal(self):
        path=self.root/'log'
        path.write_text('x'*privacy.MAX_LOG+'\n'+('[q4t][http] status=200\n'*520)+'[q4t][http] status=PRIVATE')
        value=privacy.read_log(path)
        self.assertTrue(value['tail_only']); self.assertEqual(len(value['events']),512)
        self.assertEqual(value['omitted_older_events'],8)
        link=self.root/'link';link.symlink_to(path)
        with self.assertRaises(InvalidObservation):privacy.read_log(link)

    def test_rotation_is_unknown(self):
        path=self.root/'log';path.write_text('[q4t][http] status=200\n')
        original=os.read
        def read(fd,size):
            part=original(fd,size)
            path.rename(self.root/'old');path.write_text('replacement')
            return part
        with patch.object(privacy.os,'read',side_effect=read):
            with self.assertRaisesRegex(InvalidObservation,'log_rotated_or_truncated'):
                privacy.read_log(path)

    def test_private_immutable_bundle_integrity(self):
        path=self.root/'bundle'; value=report();bundle.write(path,value)
        self.assertEqual(bundle.read(path),value)
        self.assertEqual(path.stat().st_mode & 0o777,0o700)
        self.assertEqual((path/'report.json').stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError):bundle.write(path,value)
        (path/'report.json').write_text('{}')
        with self.assertRaisesRegex(InvalidObservation,'bundle_integrity'):bundle.read(path)

    def test_public_permission_extra_file_and_symlink_refused(self):
        for kind in ('public','extra','link'):
            path=self.root/kind;bundle.write(path,report())
            if kind=='public':(path/'report.json').chmod(0o644)
            elif kind=='extra':(path/'secret').write_text('preserve')
            else:
                (path/'report.json').rename(self.root/'outside')
                (path/'report.json').symlink_to(self.root/'outside')
            with self.assertRaises(InvalidObservation):bundle.read(path)

    def test_retention_dry_run_then_owned_apply_preserves_unknown(self):
        now=time.time()
        for name,age in [('old',20*86400),('middle',100),('new',10)]:
            value=report();value['unix_seconds']=now-age;bundle.write(self.root/name,value)
        (self.root/'unknown').mkdir();(self.root/'unknown/keep').write_text('preserve')
        (self.root/'link').symlink_to(self.root/'new',target_is_directory=True)
        plan=retention.plan(self.root,max_bundles=1,now=now)
        self.assertEqual({r['name'] for r in plan['remove']},{'old','middle'})
        self.assertFalse(plan['destructive']);self.assertTrue((self.root/'old').exists())
        self.assertEqual(retention.apply(self.root,plan)['deleted_count'],2)
        self.assertTrue((self.root/'new').exists());self.assertTrue((self.root/'link').is_symlink())
        self.assertEqual((self.root/'unknown/keep').read_text(),'preserve')

    def test_retention_change_aborts_before_deleting_any_bundle(self):
        for name in ('one','two'):
            value=report();value['unix_seconds']-=20*86400;bundle.write(self.root/name,value)
        plan=retention.plan(self.root)
        (self.root/'one/report.json').write_text('{}')
        with self.assertRaisesRegex(InvalidObservation,'retention_bundle_changed'):
            retention.apply(self.root,plan)
        self.assertTrue((self.root/'two/report.json').exists())

    def test_retention_limits_and_root_replacement_refused(self):
        value=report();value['unix_seconds']-=20*86400;bundle.write(self.root/'old',value)
        plan=retention.plan(self.root);plan['root_identity']['inode']+=1
        with self.assertRaisesRegex(InvalidObservation,'retention_root_changed'):
            retention.apply(self.root,plan)
        self.root.chmod(0o755)
        with self.assertRaises(InvalidObservation):retention.plan(self.root)


if __name__=='__main__':
    unittest.main()
