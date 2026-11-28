"""Live primary and light business with real managed argv and persistent state."""
import argparse
import contextlib
import hashlib
import http.client
import json
import shutil
import socket
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import responses_acceptance as live
from tools.serve_live import profile_qualification, worker_binding_acceptance as workers
from tools.serve_live import media_create_acceptance as media
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_recovery import profile_acceptance as qualify
from tools.serve_service import package as pkg

PROFILES = ('primary', 'light')
SCHEMA = 'q4t-live-primary-light-business-v1'


def discovery_candidate(build, previous, witness, expected):
    # Only a declaration-only rebuild can carry forward the previous worker
    # failure diagnosis. The fresh engine still executes both real profiles.
    prior = pkg.read(previous)
    old_protocols = {v['protocol'] for v in prior['profiles'].values()}
    pkg.require(prior['engine_sha256'] == expected['engine_sha256'] and
                prior['environment']['status'] == 'PASS' and len(old_protocols) == 1 and
                set(prior['profiles']) == set(PROFILES), 'prior two-profile qualification')
    compiled = pkg.read(build/'generated/q4t/server/chat_protocol_sources.json')
    pkg.require(all(pkg.sha(ROOT/n) == h for n,h in compiled.items()),
                'build receipt differs from current sources')
    name = 'src/server/chat_capabilities.cpp'
    original = dict(compiled)
    original[name] = witness['sources'][name]
    def protocol(rows):
        return hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(rows.items())).encode()).hexdigest()
    pkg.require(original[name] != compiled[name] and protocol(original) in old_protocols,
                'compiled protocol change is broader than discovery')
    return dict(previous_binary=expected['engine_sha256'], previous_protocol=protocol(original),
                binary=pkg.sha(build/'q4t'), protocol=protocol(compiled), changed_source=name)


def prepare(out, selection, *, discovery_build=None, previous_profiles=None):
    previous = ROOT/'build/service'
    envelope = pkg.read(previous/'manifest.json')
    selected = pkg.verify(previous/'engine', envelope['engine_manifest_sha256'])
    qualified = pkg.read(selection)
    expected = qualified['observation']['workers']
    pkg.require(qualified['environment']['status'] == 'PASS' and
                qualified['observation']['restored'] == 201 and
                qualified['observation']['same_model'], 'worker business qualification')
    witness = pkg.read(selection.parent/'plan.json')
    pkg.require(witness['binary_sha256'] == expected['engine_sha256'] and
                witness['live_workers'] == expected, 'worker witness binary binding')
    pkg.require((discovery_build is None) == (previous_profiles is None),
                'discovery rebuild and prior profiles must be supplied together')
    change = None
    if discovery_build is not None:
        change = discovery_candidate(discovery_build, previous_profiles, witness, expected)
        expected = dict(expected, engine_sha256=change['binary'])
    prepare_profiles(out, envelope, selected, expected, witness['ingress_sha256'],
                     discovery_change=change)


def prepare_profiles(out, envelope, selected, expected, ingress_sha256, *,
                     discovery_change=None, beta_change=None, agent_change=None,
                     protected=None, build=None, installed_workers=None,
                     previous_fixture=None, selected_manifest=None):
    pkg.require(not out.exists(), 'fresh stage required')
    out.mkdir(mode=0o700, parents=True)
    state = out/'state'
    state.mkdir(mode=0o700)
    receipt = out/'identity.json'
    pkg.save(receipt, dict(engine_sha256=expected['engine_sha256'],
                          ingress_sha256=ingress_sha256))
    ports = []
    with contextlib.ExitStack() as stack:
        for _ in range(3 * len(PROFILES)):
            sock = stack.enter_context(socket.socket())
            sock.bind(('127.0.0.1', 0))
            ports.append(sock.getsockname()[1])
    for index, name in enumerate(PROFILES):
        phase = out/name
        live.prepare(phase, receipt, protected=protected, build=build, previous_fixture=previous_fixture)
        plan = pkg.read(phase/'plan.json')
        plan['port'], plan['operations_port'], plan['public_port'] = ports[3*index:3*index+3]
        workers.Stage.prepare_fixture(phase, plan, installed_workers)
        pkg.require(plan['live_workers'] == expected, 'media/engine qualification drift')
        package_dir = Path(plan['binary']).parent
        shutil.copy2(phase/'client-policy.json', package_dir/'client-policy.json')
        # Preserve accepted model/backend selections and both exact capacities.
        # Each stage has a fresh client policy and shared private object root.
        core = selected.copy()
        pkg.require(not (set(core) & {'mcp_policy','web_policy','webhook_policy','access_program_policy',
                                     'file_input_policy'}), 'additional authorities need isolation')
        settings = dict(core['settings'], profile=envelope['recovery_profiles']['profiles'][name],
                        port=plan['port'], operations_port=plan['operations_port'])
        settings['application_state'] = dict(settings['application_state'], directory='', live_recordings=True)
        core.update(settings=settings, live_workers=expected)
        command = pkg.command(package_dir, core, state_directory=state)
        pkg.require(command[0] == plan['binary'] and '--live-recordings' in command and
                    '--live-worker-sha256' in command, 'managed model command')
        plan.update(schema=SCHEMA, limits=Stage.limits, name=name, command=command,
                    discovery_change=discovery_change,
                    capacity=settings['profile'], selected_sha256=pkg.sha(selected_manifest or ROOT/'build/service/manifest.json'),
                    installed_policy_sha256=pkg.sha(package_dir/'client-policy.json'))
        if beta_change is not None:
            plan['beta_change'] = beta_change
            helper = 'tools/serve_responses/beta_profile_acceptance.py'
            plan['sources'][helper] = pkg.sha(ROOT/helper)
        if agent_change is not None:
            plan['agent_change'] = agent_change
            helper = 'tools/serve_responses/agent_profile_acceptance.py'
            plan['sources'][helper] = pkg.sha(ROOT/helper)
        pkg.save(phase/'plan.json', plan)


class Stage(workers.ice.Stage):
    limits = dict(starts=1, ingress_starts=1, sdk_posts=1, chats=1,
                  controls=2000, seconds=300)

    def model_command(self):
        pkg.require(pkg.sha(Path(self.plan['binary']).parent/'client-policy.json') ==
                    self.plan['client_policy_sha256'] == self.plan['installed_policy_sha256'],
                    'client policy drift')
        return self.plan['command']

    def api(self, method, path, *, client='alpha', body=None, agent_beta=False, timeout=None):
        ctx = ssl.create_default_context(cafile=self.plan['tls']['certificate'])
        conn = http.client.HTTPSConnection('127.0.0.1', self.plan['public_port'],
                                           context=ctx, timeout=timeout if timeout is not None
                                           else (45 if agent_beta else 10))
        try:
            headers = {'Authorization':'Bearer '+self.keys[client], 'Content-Type':'application/json'}
            if agent_beta:
                headers['OpenAI-Beta'] = 'responses_multi_agent=v1'
            conn.request(method, path, None if body is None else json.dumps(body),
                         headers)
            response = conn.getresponse()
            raw = response.read(65537)
            pkg.require(len(raw) <= 65536, 'conversation response bound')
            return response.status, json.loads(raw)
        finally:
            conn.close()

    def business(self):
        name = self.plan['name']
        status, raw = self.http('/v1/config', label='configuration')
        cfg = json.loads(raw)
        cap = self.plan['capacity']
        pkg.require(status == 200 and cfg['runtime']['mtp_enabled'] is cap['mtp_enabled'] and
                    cfg['runtime']['media_enabled'] is cap['allow_media'] and
                    cfg['application_state']['live_recordings'] and
                    cfg['live_worker_profile'] == 'q4t-live-workers-v1', 'effective Live/MTP config')
        status, capabilities = self.api('GET', '/v1/capabilities', client='gamma')
        pkg.save(self.out/'capabilities.json', capabilities)
        pkg.require(status == 200 and capabilities.get('protocol_source_sha256') ==
                    cfg['protocol_source_sha256'], 'capability identity: '+str(status))
        if 'overlap_transcription_handoff' in self.plan:
            from tools.native_asr import overlap_profiles
            overlap_profiles.business(self)
        if 'responses_stream_text_handoff' in self.plan:
            from tools.serve_audit import stream_text_profiles
            stream_text_profiles.business(self)
        if 'web_provider_handoff' in self.plan:
            from tools.serve_responses import web_provider_profiles
            web_provider_profiles.business(self)
        if 'batch_cancellation_handoff' in self.plan:
            from tools.serve_audit import batch_cancel_profiles
            batch_cancel_profiles.business(self)
        if 'standalone_diagnostic_output' in self.plan:
            from tools.serve_audit import diagnostic_profiles
            diagnostic_profiles.business(self)
        if 'concurrent_upload_completion' in self.plan:
            from tools.serve_responses import upload_completion_profiles
            upload_completion_profiles.business(self)
        if 'chatkit_pending_browser_handoff' in self.plan:
            from tools.serve_responses import chatkit_pending_profiles
            chatkit_pending_profiles.business(self)
        if 'asset_reader_retrieval' in self.plan:
            from tools.native_embedding import asset_retrieval_profiles
            asset_retrieval_profiles.business(self)
        if 'cursor_page_handoff' in self.plan:
            from tools.serve_responses import cursor_profiles
            cursor_profiles.business(self)
        if 'agent_turn_cache_handoff' in self.plan:
            from tools.serve_responses import agent_turn_cache_profiles
            agent_turn_cache_profiles.business(self)
        if 'chatkit_handoff' in self.plan:
            from tools.serve_responses import chatkit_profiles
            chatkit_profiles.business(self, capabilities)
        if 'environment_handoff' in self.plan or 'preparation_handoff' in self.plan:
            from tools.serve_responses import environment_lifecycle_profiles
            environment_lifecycle_profiles.business(self, capabilities)
        if 'webhook_handoff' in self.plan:
            self.webhook_subscriber.business(self, capabilities)
        if 'automation_handoff' in self.plan:
            from tools.serve_responses import automation_profiles
            automation_profiles.business(self, capabilities)
        if 'translation_transport' in self.plan:
            from tools.serve_realtime import translation_profiles
            translation_profiles.business(self, capabilities)
        if 'code_handoff' in self.plan:
            from tools.serve_responses import code_profiles
            code_profiles.business(self, capabilities)
        if 'skill_handoff' in self.plan:
            from tools.serve_responses import skill_profiles
            skill_profiles.business(self, capabilities)
        if 'assistant_handoff' in self.plan:
            from tools.serve_responses import assistant_profiles
            assistant_profiles.business(self, capabilities)
        if 'media_client_change' in self.plan:
            from tools.native_asr import translation_profiles
            translation_profiles.business(self, capabilities)
        if self.plan.get('agent_vaults', {}).get('subagents'):
            from tools.serve_responses import subagent_profile_acceptance
            subagent_profile_acceptance.business(self)
        if self.plan.get('discovery_change'):
            pkg.require(cfg['protocol_source_sha256'] == self.plan['discovery_change']['protocol'] and
                        capabilities['live']['sideband'] == '/v1/live/sessions/{session_id}/attach' and
                        capabilities['realtime']['client_secrets']['owner_scoped'] and
                        capabilities['realtime']['calls']['create'] == '/v1/realtime/calls' and
                        capabilities['realtime']['pending_execution'] == [],
                        'actual Live discovery differs')
        if 'beta_change' in self.plan:
            from tools.serve_responses import beta_profile_acceptance
            beta_profile_acceptance.business(self, capabilities)
        if 'agent_interruption' in self.plan:
            from tools.serve_responses import interruption_profile_acceptance
            interruption_profile_acceptance.business(self)
        if 'agent_change' in self.plan:
            from tools.serve_responses import agent_profile_acceptance
            agent_profile_acceptance.business(self, capabilities)
        if 'agent_registration' in self.plan:
            from tools.serve_responses import registration_profile_acceptance
            registration_profile_acceptance.business(self, capabilities)
        if 'agent_sessions' in self.plan:
            from tools.serve_responses import session_profile_acceptance
            session_profile_acceptance.business(self, capabilities)
        shared = self.out.parent/'conversation.json'
        if name == 'primary':
            status, value = self.api('POST', '/v1/conversations',
                body=dict(metadata=dict(check='managed_live_profiles')))
            pkg.require(status in (200,201) and value['object'] == 'conversation', 'persistent conversation create')
            pkg.save(shared, dict(id=value['id'], metadata=value['metadata']))
        else:
            previous = pkg.read(shared)
            target = '/v1/conversations/'+previous['id']
            status, value = self.api('GET', target)
            pkg.require(status == 200 and value['metadata'] == previous['metadata'] and
                        value['id'] == previous['id'], 'persistent conversation after profile change')
            pkg.require(self.api('GET', target, client='beta')[0] == 404,
                        'conversation owner separation')
        media.rtc.Gst.init(None)
        session = dict(model='qwen3.8-flash-next',
                       client=dict(data_channel=dict(allowed_client_events=['session.close'])))
        with media.rtc.peer_offer('OPUS') as (peer, _, _, channel, messages, offer, _):
            reply = self.create(offer, session, client='beta')
            pkg.require(reply['status'] == 201, 'SDK WebRTC create: '+str(reply.get('code')))
            ident = reply['data']['session']['id']
            media.rtc.accept_answer(peer, reply['data']['transport']['sdp'])
            def observed(kind):
                return any(x['type'] == kind and x.get('session', {}).get('id') == ident
                           for x in map(json.loads, list(messages)))
            media.rtc.wait_for(lambda: observed('session.started'), 'session start', 15)
            channel.send_string(json.dumps(dict(type='session.close', event_id='finish')))
            media.rtc.wait_for(lambda: observed('session.closed'), 'session close', 15)
        if 'agent_vaults' in self.plan:
            from tools.serve_responses import vault_profile_acceptance
            vault_profile_acceptance.business(self, capabilities)
        self.core('core-after')
        self.snapshot('drained')
        if {'chatkit_actions_handoff', 'chatkit_browser_output_handoff',
            'chatkit_generation_handoff', 'chatkit_workflow_handoff',
            'chatkit_image_handoff', 'chatkit_widget_handoff', 'agent_session_control_handoff',
            'response_media_handoff', 'visual_answer_handoff', 'visual_frame_handoff', 'eval_cancel_handoff',
            'response_failure_handoff', 'video_clip_handoff', 'quickdraw_region_handoff', 'tool_map_handoff',
            'null_assistant_history_handoff', 'stored_parts_handoff', 'conversation_compaction_handoff',
            'approval_identifiers_handoff', 'local_shell_handoff', 'client_patch_handoff', 'client_shell_handoff', 'program_fork_handoff',
            'video_track_grid_handoff', 'chatkit_interrupted_retry_handoff', 'video_layer_handoff', 'cache_retention_handoff',
            'mcp_error_sdk_handoff', 'compact_window_handoff', 'compact_media_handoff',
            'program_sdk_reader_handoff', 'node_sdk_reader_handoff'}.intersection(self.plan):
            from tools.serve_responses import chatkit_actions_profiles
            chatkit_actions_profiles.business(self, capabilities)
        if 'chatkit_retry_handoff' in self.plan:
            from tools.serve_responses import chatkit_retry_profiles
            chatkit_retry_profiles.business(self, capabilities)
        if 'stored_refusal_handoff' in self.plan:
            from tools.serve_chat_storage import refusal_profiles
            refusal_profiles.business(self, capabilities)
        if 'typed_history_handoff' in self.plan:
            from tools.serve_protocol import typed_history_profiles
            typed_history_profiles.business(self, capabilities)
        if 'alternate_track_handoff' in self.plan:
            from tools.serve_responses import video_track_profiles
            video_track_profiles.business(self, capabilities)
        if 'video_character_handoff' in self.plan:
            from tools.serve_responses import video_character_profiles
            video_character_profiles.business(self, capabilities)
        if 'chatkit_dictation_handoff' in self.plan:
            from tools.serve_responses import chatkit_dictation_profiles
            chatkit_dictation_profiles.business(self, capabilities)
        if 'assistant_pagination_handoff' in self.plan:
            from tools.serve_responses import assistant_pagination_profiles
            assistant_pagination_profiles.business(self, capabilities)
        if 'chatkit_upload_handoff' in self.plan:
            from tools.serve_responses import chatkit_upload_profiles
            chatkit_upload_profiles.business(self, capabilities)
        if 'chatkit_feedback_handoff' in self.plan:
            from tools.serve_responses import chatkit_feedback_profiles
            chatkit_feedback_profiles.business(self, capabilities)
        if 'eval_meteor_handoff' in self.plan:
            from tools.serve_responses import eval_meteor_profiles
            eval_meteor_profiles.business(self, capabilities)
        if 'history_arguments_handoff' in self.plan:
            from tools.serve_protocol import history_argument_profiles
            history_argument_profiles.business(self, capabilities)
        if 'project_safety_handoff' in self.plan:
            from tools.serve_responses import safety_profiles
            safety_profiles.business(self, capabilities)
        if 'eval_handoff' in self.plan or 'eval_cosine_handoff' in self.plan:
            from tools.serve_responses import eval_profiles
            eval_profiles.business(self, capabilities)
        pkg.save(self.out/'observation.json', dict(profile=name, protocol=cfg['protocol_source_sha256'],
            capacity=cap, mtp_enabled=cfg['runtime']['mtp_enabled'], returned_chat='42',
            media_created=201, session_closed=True, recordings_enabled=True,
            conversation='created' if name == 'primary' else 'recovered', worker_files=self.plan['live_workers']['files']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare','run','finalize','qualify'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--worker-qualification', type=Path)
    parser.add_argument('--discovery-build', type=Path)
    parser.add_argument('--previous-profiles', type=Path)
    parser.add_argument('--accepted-sha256')
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    pkg.require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory')
    if args.action == 'qualify':
        source = out/'acceptance.json'
        pkg.require(args.receipt is not None and args.accepted_sha256 == pkg.sha(source),
                    'qualification requires the retained acceptance digest and a new receipt path')
        destination = args.receipt.resolve()
        pkg.require(destination.is_relative_to(ROOT/'.q4t-work') and not destination.exists(),
                    'qualification receipt needs an unused private path')
        value = profile_qualification.observations(out, pkg.read(source))
        pkg.require(pkg.sha(source) == args.accepted_sha256, 'acceptance changed during review')
        pkg.save(destination, dict(value, acceptance_sha256=args.accepted_sha256))
        return
    if args.action == 'prepare':
        prepare(out, args.worker_qualification, discovery_build=args.discovery_build,
                previous_profiles=args.previous_profiles)
        return
    plans = [pkg.read(out/name/'plan.json') for name in PROFILES]
    pkg.require(all(p['schema'] == SCHEMA for p in plans), 'profile plan identity')
    if args.action == 'run':
        with base.lifecycle.child_subreaper():
            subscriber = None
            if any('webhook_handoff' in p for p in plans):
                from tools.serve_responses.webhook_profiles import Subscriber
                pkg.require(all('webhook_handoff' in p for p in plans), 'incomplete webhook handoff')
                subscriber = Subscriber()
            for plan in plans:
                if 'agent_interruption' in plan:
                    from tools.serve_responses import interruption_profile_acceptance
                    interruption_profile_acceptance.seed(Path(plan['output']), plan)
                if ('eval_handoff' in plan or 'eval_cosine_handoff' in plan) and plan['name'] == 'light':
                    from tools.serve_responses import eval_profiles
                    eval_profiles.capture_stopped_run(Path(plan['output']), plan)
                stage = Stage(plan)
                if subscriber:
                    stage.webhook_subscriber = subscriber
                stage.run()
        return
    # Development-only release validation. Neither the model nor its guardian
    # imports the schema validator.
    from jsonschema import Draft202012Validator
    schema = Draft202012Validator(pkg.read(ROOT/'docs/serve-config-v1.schema.json'))
    bundle = pkg.read_openapi(ROOT/'docs/serve-openapi-bundle-v1.json')
    bundled = Draft202012Validator({'components':bundle['components'],
                                   '$ref':'#/components/schemas/serve-config-v1.schema.json'})
    for name in PROFILES:
        observed = pkg.read(out/name/'configuration.wire')
        schema.validate(observed)
        bundled.validate(observed)
    results = [pkg.read(out/n/'result.json') for n in PROFILES]
    pkg.require(all(r['status']=='PASS' and r['default_unchanged'] and
                r['seconds'] <= Stage.limits['seconds'] and
                all(r['counts'][k] == 1 for k in ('starts','ingress_starts','sdk_posts','chats'))
                for r in results), 'profile business or resource bound')
    env = qualify.environment(pkg.read(out/'lease/exit.json'),
        [json.loads(s) for s in (out/'lease/nvml-queries.jsonl').read_text().splitlines()],
        [pkg.read(out/n/'cleanup.json') for n in PROFILES])
    pkg.require(all(pkg.sha(ROOT/n) == h for p in plans for n,h in p['sources'].items()),
                'verified sources changed')
    accepted = dict(environment=env, results=results,
        profiles={n:pkg.read(out/n/'observation.json') for n in PROFILES},
        engine_sha256=plans[0]['binary_sha256'], ingress_sha256=plans[0]['ingress_sha256'],
        disclosure_update=plans[0].get('discovery_change'))
    value = profile_qualification.observations(out, accepted)
    pkg.save(out/'acceptance.json', accepted)
    pkg.save(out/'profile-observations.json', dict(value, acceptance_sha256=pkg.sha(out/'acceptance.json')))


if __name__ == '__main__':
    main()
