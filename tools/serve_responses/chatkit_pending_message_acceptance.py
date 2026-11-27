"""Verify that the authenticated ChatKit browser can change turns before tool return."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_live import profile_acceptance as profiles
from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ACTOR = Path(__file__).with_name('chatkit_pending_message_sdk.mjs')
PROOF = ROOT/'docs/evidence/serve-chatkit-pending-user-message-20261127/native.json'
KEY = 'chatkit_pending_user_turn'


class Stage(profiles.Stage):
    def business(self):
        self.budget('sdk_posts')
        plan = self.plan
        args = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1",
                    key=self.keys['alpha'], other=self.keys['beta'], sdk=plan['sdk_directory'])
        with (self.out/'browser.stderr').open('xb') as errors:
            job = subprocess.run(['node', ACTOR], input=json.dumps(args).encode(),
                stdout=subprocess.PIPE, stderr=errors, timeout=125,
                env=dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate']))
        package.require(job.returncode == 0, 'ChatKit browser failed; see browser.stderr')
        record = json.loads(job.stdout)
        package.require(record['requests'] == 12 and record['answer'] == '42'
                        and len(set(record['history'])) == 4
                        and record['history'][1] == record['original_item']
                        and record['unchanged_pending_call'] and record['late_output'] == 409
                        and record['foreign_session'] == record['foreign_project'] == 404,
                        'incomplete browser tool interruption observation')
        package.save(self.out/'browser.json', record)
        self.budget('chats')
        status, chat = self.api('POST', '/v1/chat/completions', body=dict(
            model='qwen3.8-flash-next', temperature=0, enable_thinking=False,
            max_completion_tokens=16, messages=[dict(role='user',content='Return only 6 times 7.')]))
        package.require(status == 200 and chat['choices'][0]['message']['content'].strip() == '42',
                        'independent Chat did not return 42')
        package.save(self.out/'independent-chat.json', chat)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    prep = actions.add_parser('prepare')
    for name in ('build', 'deployment', 'release', 'output'):
        prep.add_argument('--'+name, type=Path, required=True)
    actions.add_parser('run').add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        out = args.output.resolve()
        package.require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
        lifecycle.prepare_candidate(out, args.build.resolve(), args.deployment.resolve(),
            args.release.resolve(), reuse.CURSOR_PAGE_REVIEWED, KEY, [ACTOR, Path(__file__), PROOF])
        return
    plan = package.read(args.plan)
    package.require(args.plan.resolve().is_relative_to(ROOT/'.q4t-work')
                    and plan['schema'] == profiles.SCHEMA and plan['name'] == 'primary'
                    and plan[KEY]['previous'] == reuse.CURSOR_PAGE_REVIEWED['primary']
                    and plan[KEY]['release'] == plan['selected_sha256']
                    and plan['binary_sha256'] == package.sha(plan['binary'])
                    and all(package.sha(ROOT/path) == h for path,h in plan['sources'].items()),
                    'browser acceptance plan identity changed')
    with profiles.base.lifecycle.child_subreaper():
        Stage(plan).run()


if __name__ == '__main__':
    main()
