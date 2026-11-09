import hashlib,json,os,subprocess,sys,time
from pathlib import Path
root=Path.cwd();base=root/'.q4t-work/serve-http-20261109';mode=sys.argv[1];out=base/(mode+'-01');out.mkdir()
plan=json.loads((base/'business-01/plan.json').read_bytes());env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',Q4T_NODE_SDK_DIR=plan['sdk']['sdk_directory'])
if mode=='prefix-cpu':
 cmd=[sys.executable,'-B','-m','unittest','-v','tools.serve_http.test_client_prefix.PrefixTests','tools.serve_http.test_acceptance.DriverTests.test_prefix_resume_requires_same_cleaned_failure_and_charged_counts','test_chat_client.ClientContracts.test_transport_single_post_and_matching_identity'];env['PYTHONPATH']=str(root/'tools/examples')+':'+str(root);count=4
else:cmd=[plan['sdk']['node']['path'],'--test','tools/serve_http/test_client_prefix.mjs'];count=2
(out/'command.json').write_text(json.dumps(cmd,indent=2)+'\n')
files=['tools/examples/chat_client.py','tools/examples/js/chat_client.mjs','tools/serve_http/acceptance.py','tools/serve_http/resume.py','tools/serve_http/test_acceptance.py','tools/serve_http/test_client_prefix.py','tools/serve_http/test_client_prefix.mjs']
(out/'sources.json').write_text(json.dumps({n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in files},indent=2)+'\n');start=time.monotonic()
with (out/'tests.log').open('wb') as log:code=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,timeout=60,env=env).returncode
result=dict(exit_code=code,seconds=time.monotonic()-start,tests=count,gpu_operations=0)
(out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(result);print((out/'tests.log').read_text()[-3800:])
