"""Bounded citation history on exact primary/light capacities and durable Chat.

Reuses the existing release qualification business, adding real project citation
history and public stored-message checks. No extra generation or model restart.
"""
import argparse
import copy
import json
from pathlib import Path
import signal
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import profile_acceptance as qualification
from tools.serve_service.package import read, require, save, sha

ORIGINAL_REQUESTS = qualification.requests
LIMITS = dict(starts=2, chats=10, controls=2000, seconds=420)


def history():
    source = ROOT / qualification.DOCUMENT
    text = source.read_text()[:480]
    return dict(role='assistant', content=text, annotations=[dict(
        type='url_citation', url_citation=dict(start_index=0, end_index=len(text),
            title=source.name, url='https://github.com/thomas-hiddenpeak/qwen4-thor/'
                'blob/main/' + qualification.DOCUMENT))])


def requests(media=False):
    rows = ORIGINAL_REQUESTS(media)
    for label in ('schema', 'tool'):
        rows[label]['messages'].insert(0, history())
        rows[label]['store'] = True
    return rows


qualification.requests = requests
qualification.limits_for = lambda _profile: LIMITS


class Stage(qualification.Stage):
    def __init__(self, plan):
        super().__init__(plan)
        self.created = {}

    def stored(self, name, identity):
        status, raw = self.http('/v1/chat/completions/' + identity + '/messages',
            label=name+'-stored-messages')
        require(status == 200, 'public stored Chat messages unavailable')
        data = json.loads(raw)['data']
        require(data and all(data[0][key] == value for key, value in history().items()),
                'original citation history not preserved')
        return data

    def request(self, label, body, path='/v1/chat/completions'):
        result = super().request(label, body, path)
        if path == '/v1/chat/completions':
            name = self.profile_name+'-'+label
            self.created[name] = result['id']
            self.stored(name, result['id'])
        return result

    def start(self):
        super().start()
        expected = dict(types=['url_citation'],
            offsets='original_content_unicode_characters',
            stored_message_round_trip=True, automatic_fetch=False)
        actual = read(self.out/(self.profile_name+'-capabilities.json'))
        require(actual['template']['annotation_history'] == expected,
                'actual citation history capability differs')
        if self.profile_name == 'light':
            for label, identity in self.created.items():
                self.stored('restart-'+label, identity)

    def business(self):
        super().business()
        require(len(self.created) == 6, 'missing original stored generations')
        save(self.out/'annotations-business.json', dict(
            created=self.created, history=history(), original_annotations=True,
            primary_to_light_restart=True, tool_loop_completed=True,
            generations_not_replayed=True))


def finalize(output):
    plan = read(output/'plan.json')
    records = read(output/'annotations-business.json')
    require(records['history'] == history() and len(records['created']) == 6,
            'frozen annotation history differs')
    for label, identity in records['created'].items():
        original = read(output/(label+'.request.json'))
        require(original['store'] is True and original['messages'][0] == history(),
                'actual submitted citation request differs')
        value = json.loads((output/(label+'.wire')).read_bytes())
        require(value['id'] == identity and value['usage']['completion_tokens'] > 0,
                'actual original generation differs')
        names = [label] + (['restart-'+label] if label.startswith('primary-') else [])
        for name in names:
            meta = read(output/(name+'-stored-messages.http.json'))
            data = json.loads((output/(name+'-stored-messages.wire')).read_bytes())['data']
            require(meta['status'] == 200 and data and all(
                data[0][key] == value for key, value in history().items()),
                'original stored/restart wire differs')
    require(plan['sources'][qualification.DOCUMENT] == sha(ROOT/qualification.DOCUMENT),
            'project citation source changed')
    qualification.finalize(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    prepare = modes.add_parser('prepare')
    for key in ('binary', 'candidate-build', 'package', 'output'):
        prepare.add_argument('--'+key, type=Path, required=True)
    prepare.add_argument('--port', type=int, default=18188)
    run = modes.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    final = modes.add_parser('finalize')
    final.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        qualification.prepare(args)
        path = args.output.resolve()/'plan.json'
        plan = read(path)
        plan['sources'][str(Path(__file__).resolve())] = sha(Path(__file__))
        plan['scope'] = __doc__
        plan['citation_history'] = history()
        save(path, plan)
    elif args.mode == 'finalize':
        finalize(args.output.resolve())
    else:
        def timeout(_signal, _frame):
            raise TimeoutError('citation qualification deadline')
        previous = signal.signal(signal.SIGALRM, timeout)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with qualification.encrypted.foreground.base.lifecycle.child_subreaper():
                Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    main()
