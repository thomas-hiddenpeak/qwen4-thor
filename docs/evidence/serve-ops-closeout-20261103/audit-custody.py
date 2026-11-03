"""Allow only the receipt this checker creates, then verify that link exists."""
from pathlib import Path

ROOT=next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists())
source=Path(__file__).with_name('custody.py')
receipt=ROOT/'docs/evidence/serve-ops-custody-20261103.json'
assert not receipt.exists()
raw=source.read_text()
old="if not (document.parent/target).exists():missing.append(dict(source=str(document),target=target))"
new="""if not (document.parent/target).exists():
            if (document.parent/target).resolve() == ROOT/'docs/evidence/serve-ops-custody-20261103.json':
                continue  # This exact receipt is created only after every other gate passes.
            missing.append(dict(source=str(document),target=target))"""
assert raw.count(old)==1
raw=raw.replace(old,new)
old="receipt=dict(stage_custody_passed=True,"
new="""receipt=dict(original_checker=h.identity(stage.BASE/'custody.py'),
    original_checker_failure='required its own not-yet-created receipt; raw traceback in tool transcript',
    self_receipt_link_deferred_until_atomic_check_completion=True,
    corrected_checker=h.identity(stage.BASE/'audit-custody.py'),stage_custody_passed=True,"""
assert raw.count(old)==1
raw=raw.replace(old,new)
exec(compile(raw,str(source),'exec'),dict(__file__=str(source),__name__='__main__'))
assert receipt.is_file()
