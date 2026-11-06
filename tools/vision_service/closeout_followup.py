"""Frozen nine-request follow-up; reuse passed images and actual tool read."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess

from tools.vision_service import closeout as c
from tools.examples.vision_query import with_json_schema

s = c.s
FOLDER = c.BASE / "phase-b"
LIMITS = dict(starts=1, generations=9, controls=30, native=0, seconds=600)
SCHEMA = s.ROOT / "tools/examples/vision_direction.schema.json"


def prepare():
    prior = c.read(c.FOLDER/"execution/result.json")
    s.require(prior['counts'] == dict(starts=1,generations=17,controls=17,native=0)
        and not prior['cleanup_errors'] and not prior['monitor_failures'], 'prior phase unclean')
    s.require(c.read(c.FOLDER/'execution/record-tool-final-transport.json')['status']==502,
              'unexpected prior final result')
    FOLDER.mkdir()
    rows = []
    for row in c.read(c.FOLDER/"requests.json"):
        if row.get('mode') != 'strict':continue
        body = c.fresh(row['request'])
        body['response_format']['json_schema']['schema']['properties']['direction']['enum'] = [
            'left','right','stationary']
        expected = dict(direction='left' if row['group']=='motion-reverse' else 'right')
        rows.append(dict(label=row['group']+'-simple',request=body,expected=expected,
                         contract='renamed enum; original prompt unchanged'))
        body = c.fresh(row['request'])
        body.pop('response_format')
        body = with_json_schema(body,c.read(SCHEMA))
        rows.append(dict(label=row['group']+'-explicit',request=body,expected=row['expected'],
                         contract='application schema in prompt; original failing request not fixed'))
    # Resume only the failed response with the actual previous tool result.
    # No fabricated result and no repeated tool execution.
    original_final = c.read(c.FOLDER/'execution/record-tool-final-request.json')['body']
    follow = c.fresh(original_final)
    follow['max_tokens'] = 768
    rows.append(dict(label='record-tool-final-768',request=follow,
        expected=c.read(c.FOLDER/'fixtures/truth.json')['totals'],
        contract='same actual tool history, only output budget increased'))
    s.save(FOLDER/'requests.json',rows)
    paths = [s.ROOT/n for n in subprocess.check_output(['git','ls-files'],cwd=s.ROOT,text=True).splitlines()
        if n.startswith(('src/','include/','cmake/','tools/','docs/serve-')) or n=='CMakeLists.txt']
    paths += [FOLDER/'requests.json', c.FOLDER/'plan.json', c.BASE/'phase-a-originals.json',
        c.FOLDER/'execution/result.json', c.FOLDER/'execution/record-tool-result.json',
        c.FOLDER/'execution/record-tool-call-completion.json',
        c.FOLDER/'execution/record-tool-final-request.json',
        s.ROOT/c.RECORD, s.ROOT/'docs/VISION_CLOSEOUT_PLAN_2026-11-07.md']
    plan = copy.deepcopy(c.read(c.FOLDER/'plan.json'))
    paths += [Path(plan['binary']),s.ROOT/'build/q4t.release.json']
    paths += [s.MODEL/n for n in ('config.json','preprocessor_config.json',
                                'video_preprocessor_config.json','tokenizer.json')]
    plan.update(profile='q4t-visual-closeout-b-v1', limits=LIMITS,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=s.ROOT,text=True).strip(),
        files={str(p):s.sha(p) for p in paths})
    c.budget_plans([c.read(p) for p in c.BASE.glob('*/plan.json')] + [plan])
    s.save(FOLDER/'plan.json',plan)
    return dict(requests=len(rows),limits=LIMITS)


class Run(s.Run):
    limits = LIMITS
    success_status = 'PASS_SCOPED_EXPLICIT_SCHEMA_AND_REAL_RECORD_LOOP'

    def business(self):
        c.budget_plans([c.read(p) for p in c.BASE.glob('*/plan.json')])
        for row in c.read(self.folder/'requests.json'):
            label, body = row['label'],row['request']
            pre = json.loads(self.http(label+'-preflight','/v1/chat/preflight',dict(
                request=body,include_prompt=True,include_token_ids=True)))
            value = self.complete(label,body)
            s.require(value.finish_reason=='stop','incomplete '+label)
            s.require(value.usage['prompt_tokens']==pre['prompt_tokens'],'prompt usage mismatch')
            content = value.message.get('content','')
            if 'response_format' in body:
                actual = json.loads(content)
                s.Draft202012Validator(body['response_format']['json_schema']['schema']).validate(actual)
                passed = actual == row['expected']
            else:
                source = c.read(c.FOLDER/'execution/record-tool-result.json')['source']
                passed = source['citation'] in content
            self.observe(label,passed,expected=row['expected'],actual=content,
                         contract=row['contract'],usage=value.usage)
        self.idle('final-idle')


if __name__ == '__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run'))
    print(json.dumps(prepare() if parser.parse_args().action=='prepare' else Run(FOLDER).run()))
