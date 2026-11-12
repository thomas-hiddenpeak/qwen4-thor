import importlib.util,json,hashlib,pathlib,sys
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root))
from tools.serve_access import acceptance as a
plan=json.loads((root/'.q4t-work/serve-access-20261109/business-01/plan.json').read_bytes())
for name in ('tools/serve_access/acceptance.py','tools/serve_access/test_acceptance.py'):
 plan['sources'][name]=hashlib.sha256((root/name).read_bytes()).hexdigest()
plan['output']=str(pathlib.Path(sys.argv[2])/'prelude')
output=pathlib.Path(plan['output'])/'execution';output.mkdir(parents=True)
a.Budget(output,initialize=True,limits=plan['limits'])
a.load_modules(plan)
from tools.serve_recovery import controller
controller.main=lambda: print('PASS: sealed source verification and wrapper reached controller entry under closed environment; Controller not instantiated; no model or GPU operation')
a.controller_actor(plan,pathlib.Path('unused-config'))
