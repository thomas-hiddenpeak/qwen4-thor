"""Four frozen requests isolating the existing temporal-direction failure.

No repetitions or replacement of the failed acceptance cases. Reuses the
verified governance records and the common custody/sampling/cleanup driver.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.vision_service import stage as s

FOLDER=s.BASE/'phase-06'


def prepare():
    prior=s.BASE/'phase-05b'
    reuse=s.BASE/'cpu/governance-replay-01/result.json'
    s.require(json.loads(reuse.read_text())['status']=='PASS_SCOPED_MEDIA_GOVERNANCE','scoped governance prerequisite')
    plan=json.loads((prior/'plan.json').read_text())
    s.require(s.sha(plan['binary'])==plan['files'][plan['binary']],'runtime changed')
    FOLDER.mkdir()
    original=json.loads((s.BASE/'phase-04/requests.json').read_text())
    forward=next(r['request']['messages'][0]['content'][0] for r in original if r['label']=='motion-forward')
    probes=[('first-frame',[s.image_part(s.BASE/'fixtures/motion-00.png')],
        'Is the red circle on the left or right side of this image? Reply with one word only.'),
        ('last-frame',[s.image_part(s.BASE/'fixtures/motion-07.png')],
        'Is the red circle on the left or right side of this image? Reply with one word only.'),
        ('video-plain',[forward],
        'Describe where the red circle starts and ends, and which direction it moves. Answer briefly in English.'),
        ('endpoint-images',[s.image_part(s.BASE/'fixtures/motion-00.png'),s.image_part(s.BASE/'fixtures/motion-07.png')],
        'These are the first and last frames of a video in time order. Describe where the red circle starts and ends, and which direction it moves. Answer briefly in English.')]
    s.save(FOLDER/'requests.json',[dict(label=n,request=s.request(q,p,max_tokens=128)) for n,p,q in probes])
    paths=[ROOT/n for n in subprocess.check_output(['git','ls-files'],cwd=ROOT,text=True).splitlines()
           if n.startswith(('src/','include/','cmake/','tools/','docs/serve-')) or n=='CMakeLists.txt']
    paths += [Path(n) for n in plan['files'] if Path(n).is_file()]
    paths += [FOLDER/'requests.json',reuse]
    plan.update(files={str(p):s.sha(p) for p in paths},
        matrix=dict(diagnostic_requests=4,acceptance_quality_passed=False),
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
    s.save(FOLDER/'plan.json',plan)
    return plan['matrix']


class Run(s.Run):
    success_status='DIAGNOSTIC_RESPONSES_RECORDED_NOT_QUALITY_ACCEPTANCE'

    def business(self):
        rows=json.loads((self.folder/'requests.json').read_text())
        for row in rows:
            value=self.complete(row['label'],row['request'])
            s.require(value.finish_reason=='stop','diagnostic truncated: '+row['label'])
            self.observe(row['label'],True,diagnostic_only=True,content=value.message.get('content'))
        self.idle('final-idle')


if __name__=='__main__':
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','run'))
    print(json.dumps(prepare() if p.parse_args().action=='prepare' else Run(FOLDER).run()))
