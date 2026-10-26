"""Agent stage entry with a SHA-scoped legacy single-slot audit adapter.

No inference or performance rule changes. Candidate and heterogeneous services
retain the original independent batch-work checks.
"""
import json
from pathlib import Path
import signal

import audit_parent
import run_stage as stage


def validate(plan):
    stage.validate(plan)
    if plan.get('compatibility_resume_authorized') is not True:
        raise PermissionError('third bounded audit repair requires explicit authorization')
    if (plan.get('prior_http_completed') != 227 or plan['http_requests'] != 93
            or plan['http_loads'] != 5
            or [s['name'] for s in plan['steps']] != [
                '13-full-candidate', '14-subset-candidate', '15-subset-parent',
                '16-plain-parent', '17-plain-candidate']):
        raise ValueError('only the 93 unexecuted protection requests may resume')


def audit_server(output, count):
    step = json.loads((output / 'step.json').read_text())
    if (step['layout'] == 'single' and step['head'] in ['full', 'subset']
            and stage.legacy.sha(Path(step['binary'])) == audit_parent.PARENT_SHA):
        result = audit_parent.audit(output, count)
        stage.save(output / 'agent-audit.json', result)
        return
    stage.audit_server(output, count)


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('owned compatibility stage interrupted')

    signal.signal(signal.SIGTERM, interrupted)
    stage.reuse.validate = validate
    stage.reuse.custom_step = stage.custom_step
    stage.reuse.admission.audit_server = audit_server
    stage.reuse.main()
