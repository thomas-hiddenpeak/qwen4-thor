"""Repair only the result reader; retain every original promtool execution."""
import json
from pathlib import Path
import re
import sys

ROOT=next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists())
sys.path.insert(0,str(ROOT))
from tools.serve_ops import stage
h=stage.h;out=stage.BASE/'cpu/owner-correction'
error=(out/'old-omission.stderr').read_text()
names=re.findall(r'^    name: (.+),$',error,re.MULTILINE)
h.require(names==['idle worker ownership','idle body retained','idle slot retained'],
          'unexpected original failure')
for name in ('fixed-rules','fixed-timeseries'):
    h.require('SUCCESS' in (out/(name+'.stdout')).read_text() and
              not (out/(name+'.stderr')).read_text(),'fixed policy checks failed')
fixture=h.read(out/'fixed-tests.json')
h.require(h.sha(out/'after.rules.json')==h.sha(ROOT/'deploy/serve-ops/q4t.rules.yml'),
          'corrected policy changed after test')
h.save(out/'checker-failure.json',dict(original_wrapper_exit=1,
    original_wrapper=h.identity(stage.BASE/'owner-rule-correction.py'),
    reason='substring count of name also counted alertname; three expected rule failures were present',
    original_promtool_returncodes_from_tool_transcript={'old-omission':1,'fixed-rules':0,'fixed-timeseries':0},
    raw_promtool_outputs_retained=True,raw_wrapper_traceback='tool transcript only',
    fixed_by_reading_original_outputs=True,promtool_or_model_rerun=False))
h.save(out/'result.json',dict(passed=True,old_omission_demonstrated=True,
    affected_rules=['Q4TWorkerReapStalled','Q4TIdleResourcesRetained'],
    scenarios=len(fixture['tests']),assertions=sum(len(case['alert_rule_test']) for case in fixture['tests']),
    actual_metric_evidence={name:h.identity(stage.OUTPUT/f'business/diagnostics/{name}/report.json') for name in ('before','after')},
    corrected_rule_sha256=h.sha(ROOT/'deploy/serve-ops/q4t.rules.yml'),
    correction_source_hashes={str(p.relative_to(ROOT)):h.sha(p) for p in (
        ROOT/'tools/serve_ops/rules.py',ROOT/'tools/serve_ops/fixtures.py',ROOT/'tools/serve_ops/rule_tests.py')},
    runtime_changed=False,model_or_http_repeated=False,unaffected_15_alerts_and_2_records_reused=True,
    frozen_real_run_retained=True,actual_live_monitor_used_original_rules=True,
    checker_repair=h.identity(Path(__file__))))
print(json.dumps(dict(passed=True,scenarios=len(fixture['tests']),
    assertions=sum(len(case['alert_rule_test']) for case in fixture['tests']),original_expected_failures=names),indent=2))
