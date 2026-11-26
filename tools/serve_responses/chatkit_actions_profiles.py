"""Qualify a widget and pending form across primary and light profiles."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Callable

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import agent_session_control_profiles as session_controls
from tools.serve_responses import eval_cancel_profiles, failure_profiles, video_clip_profiles
from tools.serve_responses import frame_profiles, media_profiles, visual_profiles
from tools.serve_responses import chatkit_image_profiles as images
from tools.serve_responses import chatkit_widget_profiles as widgets
from tools.serve_responses import chatkit_workflow_profiles as workflows
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses.assistant_profiles import COMMON
from tools.serve_service import package

ROOT = lifecycle.ROOT
RELEASE = ROOT / 'docs/evidence/serve-chatkit-retry-20261125/delivery.json'
PROOF = ROOT / 'docs/evidence/serve-chatkit-actions-20261125/business.json'
FORM = ROOT / 'docs/evidence/serve-chatkit-structured-20261125/business.json'
ACTOR = Path(__file__).with_name('chatkit_actions_profile_sdk.mjs').resolve()
KEY = 'chatkit_actions_handoff'
RECEIPT = reuse.CHATKIT_ACTIONS_RECEIPT


BROWSER_RELEASE = ROOT / 'docs/evidence/serve-chatkit-actions-20261125/delivery.json'
BROWSER_PROOF = ROOT / 'docs/evidence/serve-chatkit-browser-output-20261125/business.json'
BROWSER_ACTOR = Path(__file__).with_name('chatkit_browser_profile_sdk.mjs').resolve()
BROWSER_KEY = 'chatkit_browser_output_handoff'


GENERATION_RELEASE = ROOT / 'docs/evidence/serve-chatkit-browser-output-20261125/delivery.json'
GENERATION_PROOF = ROOT / 'docs/evidence/serve-chatkit-generation-stream-20261125/combined.json'
GENERATION_ACTOR = Path(__file__).with_name('chatkit_generation_profile_sdk.mjs').resolve()
GENERATION_KEY = 'chatkit_generation_handoff'


def prepare_generation(out, build, deployment):
    proof = package.read(GENERATION_PROOF)
    binary = package.sha(build / 'q4t')
    package.require(proof['binary'] == binary and proof['cancelled'] and
                    proof['web_modes'] == ['live', 'cached'] and proof['web_requests'] == 1 and
                    proof['source_citations'] == 2 and proof['terminal_errors'] == 1,
                    'candidate lacks real interrupted generation and citation evidence')
    lifecycle.prepare_candidate(out, build, deployment, GENERATION_RELEASE, reuse.CHATKIT_BROWSER_REVIEWED,
                                GENERATION_KEY, [GENERATION_PROOF, GENERATION_RELEASE, GENERATION_ACTOR,
                                                 Path(__file__), Path(lifecycle.__file__)])


def check_generation_result(row, previous=None):
    expected = str(package.read(GENERATION_RELEASE)['publication']['publication_revision'])
    source = row['source']
    ids = {'workflow': 'agent_', 'thread': 'cthr_', 'file': 'file_', 'item': 'msg_', 'user': 'item_'}
    package.require(row['sdk'] == '7.47.0' and row['chat'] == '42' and row['revision'] == expected
                    and all(source[key].startswith(prefix) for key, prefix in ids.items())
                    and source['received'].strip() and source['partial'].startswith(source['received'])
                    and len(source['document_sha256']) == 64, 'invalid original partial or source document')
    if previous is None:
        package.require(row['phase'] == 'primary' and 'recovered' not in row,
                        'primary must leave the interrupted document review in its thread')
        return
    check_generation_result(previous)
    result = row['recovered']
    recovered_ids = result['ids']
    package.require(row['phase'] == 'light' and source == previous['source'] and
                    len(recovered_ids) == len(set(recovered_ids)) == 4 and
                    recovered_ids[:2] == [source['user'], source['item']] and
                    recovered_ids[2].startswith('item_') and recovered_ids[3].startswith('msg_') and
                    result['answer'] == expected and result['file_sha256'] == source['document_sha256'] and
                    result['peer'] == result['sibling'] == 404,
                    'document, partial message, client isolation or continued answer changed')


def prepare_browser(out, build, deployment):
    proof = package.read(BROWSER_PROOF)
    final = proof['runs']['02']
    package.require(final['run']['status'] == 'PASS' and final['run']['unchanged_default'] and
                    final['binary'] == package.sha(build/'q4t') and
                    len(set(final['review']['actual_event_kinds'])) == 12 and
                    final['review']['previous_counterexamples'] == 4 and
                    proof['cancel']['committed_cancelled_title'] is False,
                    'current engine lacks qualified browser output and cancellation')
    lifecycle.prepare_candidate(out, build, deployment, BROWSER_RELEASE, reuse.CHATKIT_ACTIONS_REVIEWED,
                                BROWSER_KEY, [BROWSER_PROOF, BROWSER_RELEASE, BROWSER_ACTOR,
                                              Path(__file__), Path(lifecycle.__file__)])


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    combined = proof['combined']
    package.require(proof['run']['status'] == combined['run']['status'] == 'PASS' and
                    combined['run']['unchanged_default'] and proof['reference_operations'] == 16 and
                    len(set(proof['candidate_operations'])) == 16 and combined['actor'] == package.sha(ACTOR)
                    and combined['engine'] == package.sha(build/'q4t') and
                    package.read(FORM)['upstream_check']['persisted_rows_validated'] == 2,
                    'missing independently checked form, action, or combined engine')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.CHATKIT_RETRY_REVIEWED,
                                KEY, [PROOF, FORM, RELEASE, ACTOR, Path(__file__), Path(lifecycle.__file__)])


def check_source(row, release):
    expected = str(package.read(release)['publication']['publication_revision'])
    package.require(row['sdk'] == '7.47.0' and row['chat'] == '42' and row['revision'] == expected,
                    'unexpected SDK, core chat or release')
    source = row['source']
    identifiers = dict(workflow='agent_', file='file_', thread='cthr_', widget='item_',
                       form='fc_', call='call_', user='item_')
    package.require(all(source[k].startswith(prefix) for k, prefix in identifiers.items())
                    and len({source[k] for k in identifiers}) == len(identifiers),
                    'missing original workflow, thread, widget or tool identity')
    return expected, source


def check_result(row, previous=None):
    expected, source = check_source(row, RELEASE)
    if previous is None:
        package.require(row['phase'] == 'primary' and 'recovered' not in row,
                        'primary must leave the form pending')
        return
    check_result(previous)
    result = row['recovered']
    ids = result['ids']
    package.require(row['phase'] == 'light' and source == previous['source'] and
                    result['foreign'] == result['deleted'] == 404 and
                    result['new_turn'] == result['untyped'] == result['duplicate'] == 409 and
                    result['answered_form'] == source['form'] and result['call'] == source['call'] and
                    result['answer'] == expected and result['widget_label'] == 'verified ' + expected and
                    len(ids) == len(set(ids)) == 4 and ids[:3] == [source['user'],source['form'],source['widget']]
                    and ids[3].startswith('msg_'), 'persisted widget, form identity or answer changed')


def check_browser_result(row, previous=None):
    expected, source = check_source(row, BROWSER_RELEASE)
    if previous is None:
        package.require(row['phase'] == 'primary' and 'recovered' not in row,
                        'primary must leave the release question pending')
        return
    check_browser_result(previous)
    r = row['recovered']
    ids = r['ids']
    package.require(row['phase'] == 'light' and source == previous['source'] and
                    r['title'] == 'Verified ' + expected and r['answer'] == expected and
                    r['removed'] == source['widget'] and r['answered_form'] == source['form'] and
                    r['call'] == source['call'] and len(ids) == len(set(ids)) == 3 and
                    ids[:2] == [source['user'], source['form']] and ids[2].startswith('msg_') and
                    r['events'] == ['stream_options','progress_update','notice','thread.item.removed','thread.updated']
                    and r['late_error'] == 'error' and r['peer'] == r['missing'] == 404 and r['duplicate'] == 409,
                    'browser stream, committed title, removed widget or completed form changed')


@dataclass(frozen=True)
class Handoff:
    key: str
    release: Path
    actor: Path
    receipt: str
    predecessor: str
    reviewed: dict
    check: Callable


def handoff(plan):
    if video_clip_profiles.KEY in plan:
        return Handoff(video_clip_profiles.KEY, video_clip_profiles.RELEASE, video_clip_profiles.ACTOR,
                       video_clip_profiles.RECEIPT, failure_profiles.RECEIPT,
                       reuse.RESPONSE_FAILURE_REVIEWED, video_clip_profiles.check)
    if failure_profiles.KEY in plan:
        return Handoff(failure_profiles.KEY, failure_profiles.RELEASE,
                       failure_profiles.ACTOR, failure_profiles.RECEIPT,
                       eval_cancel_profiles.RECEIPT, reuse.EVAL_CANCEL_REVIEWED, failure_profiles.check)
    if eval_cancel_profiles.KEY in plan:
        return Handoff(eval_cancel_profiles.KEY, eval_cancel_profiles.RELEASE,
                       eval_cancel_profiles.ACTOR, eval_cancel_profiles.RECEIPT,
                       frame_profiles.RECEIPT, reuse.FRAME_REVIEWED, eval_cancel_profiles.check)
    if frame_profiles.KEY in plan:
        return Handoff(frame_profiles.KEY, frame_profiles.RELEASE, frame_profiles.ACTOR,
                       frame_profiles.RECEIPT, visual_profiles.RECEIPT,
                       reuse.VISUAL_ANSWER_REVIEWED, frame_profiles.check)
    if visual_profiles.KEY in plan:
        return Handoff(visual_profiles.KEY, visual_profiles.RELEASE, visual_profiles.ACTOR,
                       visual_profiles.RECEIPT, media_profiles.RECEIPT,
                       reuse.MEDIA_HANDOFF_REVIEWED, visual_profiles.check)
    if media_profiles.KEY in plan:
        return Handoff(media_profiles.KEY, media_profiles.RELEASE, media_profiles.ACTOR,
                       media_profiles.RECEIPT, reuse.SESSION_CONTROL_RECEIPT,
                       reuse.SESSION_CONTROL_REVIEWED, media_profiles.check)
    if session_controls.KEY in plan:
        structured = 'failed' in plan[session_controls.KEY]
        return Handoff(session_controls.KEY, session_controls.RELEASE,
                       session_controls.STRUCTURED_ACTOR if structured else session_controls.ACTOR,
                       session_controls.RECEIPT, reuse.CHATKIT_WIDGET_RECEIPT,
                       reuse.CHATKIT_WIDGET_REVIEWED, session_controls.check_structured if structured else session_controls.check)
    if widgets.KEY in plan:
        return Handoff(widgets.KEY, widgets.RELEASE, widgets.ACTOR, widgets.RECEIPT,
                       reuse.CHATKIT_IMAGE_RECEIPT, reuse.CHATKIT_IMAGE_REVIEWED, widgets.check)
    if images.KEY in plan:
        return Handoff(images.KEY, images.RELEASE, images.ACTOR, images.RECEIPT,
                       reuse.CHATKIT_WORKFLOW_RECEIPT, reuse.CHATKIT_WORKFLOW_REVIEWED, images.check)
    if workflows.KEY in plan:
        return Handoff(workflows.KEY, workflows.RELEASE, workflows.ACTOR, reuse.CHATKIT_WORKFLOW_RECEIPT,
                       reuse.CHATKIT_GENERATION_RECEIPT, reuse.CHATKIT_GENERATION_REVIEWED, workflows.check)
    if GENERATION_KEY in plan:
        return Handoff(GENERATION_KEY, GENERATION_RELEASE, GENERATION_ACTOR, reuse.CHATKIT_GENERATION_RECEIPT,
                       reuse.CHATKIT_BROWSER_RECEIPT, reuse.CHATKIT_BROWSER_REVIEWED, check_generation_result)
    if BROWSER_KEY in plan:
        return Handoff(BROWSER_KEY, BROWSER_RELEASE, BROWSER_ACTOR, reuse.CHATKIT_BROWSER_RECEIPT,
                       reuse.CHATKIT_ACTIONS_RECEIPT, reuse.CHATKIT_ACTIONS_REVIEWED, check_browser_result)
    return Handoff(KEY, RELEASE, ACTOR, RECEIPT, reuse.CHATKIT_RETRY_RECEIPT, reuse.CHATKIT_RETRY_REVIEWED,
                   check_result)


def verify(phase, plan, capabilities):
    spec = handoff(plan)
    prior_file = phase / 'previous-profile.json'
    prior = profile_record.read(prior_file)
    name, change = plan['name'], plan[spec.key]
    expected = dict(prior['capabilities'], protocol_source_sha256=change['protocol'])
    if spec.key == video_clip_profiles.KEY:
        expected['file_inputs'] = reuse.clipped_file_inputs(expected['file_inputs'])
    if spec.key in (visual_profiles.KEY, frame_profiles.KEY):
        expected['template'] = dict(expected['template'],
                                    renderer_source_sha256=package.sha(ROOT / 'src/server/chat_template.cpp'))
    package.require(package.sha(prior_file) == change['previous'] == spec.reviewed[name]
                    and plan['selected_sha256'] == change['release'] == package.read(spec.release)['publication']['primary']
                    and prior['capacity'] == plan['capacity'] and package.same_json(expected, capabilities),
                    'unexpected change in release identity, capacity or runtime declarations')
    previous = package.read(phase.parent / 'primary' / spec.receipt) if name == 'light' else None
    spec.check(package.read(phase / spec.receipt), previous)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and spec.predecessor in inherited,
                    'missing reviewed predecessor evidence')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, spec.receipt: package.sha(phase / spec.receipt)}


def business(stage, capabilities):
    plan = stage.plan
    spec = handoff(plan)
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1",
                  key=stage.keys['alpha'], other=stage.keys['beta'], phase=plan['name'],
                  sdk=plan['sdk_directory'], model='qwen3.8-flash-next')
    if spec.key in (failure_profiles.KEY, video_clip_profiles.KEY):
        settings = plan[spec.key]
        module = failure_profiles if spec.key == failure_profiles.KEY else video_clip_profiles
        wire = 'failure-wire' if spec.key == failure_profiles.KEY else 'video-clip-wire'
        params.update(settings.get('fixture', {}), certificate=plan['tls']['certificate'])
        previous = (package.read(stage.out.parent/'primary'/spec.receipt)
                    if plan['name'] == 'light' else None)
        with (stage.out/'responses-sdk.stderr').open('xb') as errors:
            row = module.exercise(params, settings['interpreter'], errors, stage.out/wire, previous)
        package.save(stage.out/spec.receipt, row)
        verify(stage.out, plan, capabilities)
        return
    if spec.key == eval_cancel_profiles.KEY:
        params['certificate'] = plan['tls']['certificate']
        previous = (package.read(stage.out.parent / 'primary' / spec.receipt)
                    if plan['name'] == 'light' else None)
        with (stage.out / 'eval-cancel-sdk.stderr').open('xb') as errors:
            row = eval_cancel_profiles.exercise(params, plan[spec.key]['interpreters'], errors,
                                                previous, captures=stage.out / 'eval-wire')
        package.save(stage.out / spec.receipt, row)
        verify(stage.out, plan, capabilities)
        return
    if spec.actor == frame_profiles.ACTOR:
        params.update(frame_profiles.sdk_parameters(stage))
    if spec.actor == visual_profiles.ACTOR:
        params.update(visual_profiles.sdk_parameters(stage))
    if spec.actor == media_profiles.ACTOR:
        params.update(media_profiles.sdk_parameters(stage))
    if spec.actor == session_controls.STRUCTURED_ACTOR:
        params['failed'] = plan[spec.key]['failed']
    if plan['name'] == 'light':
        params['previous'] = package.read(stage.out.parent / 'primary' / spec.receipt)
    with (stage.out / 'chatkit-actions-sdk.stderr').open('xb') as err:
        run = subprocess.run(['node', spec.actor], input=json.dumps(params), text=True,
                             stdout=subprocess.PIPE, stderr=err, timeout=150,
                             env=dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate']))
    package.require(run.returncode == 0, 'ChatKit SDK failed; see chatkit-actions-sdk.stderr')
    package.save(stage.out / spec.receipt, json.loads(run.stdout))
    verify(stage.out, plan, capabilities)
