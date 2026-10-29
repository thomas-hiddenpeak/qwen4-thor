"""Review stopped data with the resource-summary schema used by this repo.

The frozen reviewer used the key 'overall'; summarize() returns 'memory'.
Apply this one schema correction without modifying any frozen input or gate.
The exact corrected source is also saved in the evidence directory.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/session-incremental-20261029/continuation'
source = Path(__file__).with_name('analyze_continuation.py')
plan = json.loads((BASE/'plan.json').read_text())
original = source.read_bytes()
if hashlib.sha256(original).hexdigest() != plan['files'][str(source)]:
    raise RuntimeError('Frozen analysis source changed')
text = original.decode()
if text.count("resources['overall']") != 1:
    raise RuntimeError('Unexpected resource schema correction scope')
corrected = text.replace("resources['overall']", "resources['memory']")
target = BASE/'review-source.py'
target.write_text(corrected)
(BASE/'review-source.json').write_text(json.dumps(dict(
    correction="Resource summary key overall -> memory; gates unchanged.",
    frozen_sha256=hashlib.sha256(original).hexdigest(),
    reviewed_sha256=hashlib.sha256(corrected.encode()).hexdigest()), indent=2)+'\n')
exec(compile(corrected, str(source), 'exec'),
     {'__file__': str(source), '__name__': '__main__'})
