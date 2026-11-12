import json,subprocess,sys
from pathlib import Path
p=Path(__file__).resolve().parent
v=json.loads((p/'cpu-plan-01.json').read_text())
raise SystemExit(subprocess.call([sys.executable,'-m','unittest','-v',*v['tests']]))
